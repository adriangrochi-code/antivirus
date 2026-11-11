"""Herramientas del agente, confinadas a la carpeta del proyecto.

Las de solo lectura se ejecutan directamente; las que modifican archivos o
ejecutan comandos requieren aprobación del usuario (ver ``NEEDS_APPROVAL``).
Los comandos se ejecutan dentro de bubblewrap: el sistema es de solo lectura,
solo la carpeta del proyecto es escribible y no hay red (salvo que se permita).
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import harness

MAX_OUTPUT = 20_000
SKIP_DIRS = {".git", ".tisma", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", "target", ".cache"}
NEEDS_APPROVAL = {"write_file", "edit_file", "run_command"}


class ToolError(Exception):
    pass


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n… [{len(text) - limit} caracteres omitidos] …\n" + text[-limit // 2:]


class Workspace:
    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ToolError(f"No es una carpeta: {self.root}")
        self.allow_network = False

    def resolve(self, path: str) -> Path:
        p = (self.root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"Ruta fuera del proyecto: {path}")
        return p

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)) or "."

    # ------------------------------------------------------------ lectura
    def list_dir(self, path: str = ".", depth: int = 2) -> str:
        base = self.resolve(path)
        lines: list[str] = []

        def walk(d: Path, level: int) -> None:
            try:
                entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError as e:
                lines.append(f"{'  ' * level}[error: {e}]")
                return
            for e in entries:
                if e.name in SKIP_DIRS or len(lines) > 500:
                    continue
                if e.is_dir() and not e.is_symlink():
                    lines.append(f"{'  ' * level}{e.name}/")
                    if level + 1 < depth:
                        walk(e, level + 1)
                else:
                    lines.append(f"{'  ' * level}{e.name}")

        walk(base, 0)
        return "\n".join(lines) or "(vacía)"

    def read_file(self, path: str, offset: int = 1, limit: int = 400) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"No existe el archivo: {path}")
        text = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(offset, 1)
        chunk = text[start - 1: start - 1 + limit]
        out = "\n".join(f"{i:>5}\t{line}" for i, line in enumerate(chunk, start))
        if start - 1 + limit < len(text):
            out += f"\n… ({len(text)} líneas en total; usa offset para seguir)"
        return out or "(archivo vacío)"

    def search(self, pattern: str, glob: str = "*") -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Expresión regular inválida: {e}")
        hits: list[str] = []
        for dirpath, dirnames, files in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in files:
                if not fnmatch.fnmatch(f, glob):
                    continue
                p = Path(dirpath) / f
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if rx.search(line):
                            hits.append(f"{self.rel(p)}:{n}: {line.strip()[:200]}")
                            if len(hits) >= 200:
                                return "\n".join(hits) + "\n… (máximo de 200 resultados)"
                except OSError:
                    continue
        return "\n".join(hits) or "Sin resultados"

    # ------------------------------------------------------------ escritura (con aprobación)
    def preview_write(self, path: str, content: str) -> str:
        p = self.resolve(path)
        old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        return self._diff(path, old, content)

    def write_file(self, path: str, content: str) -> str:
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Escrito {self.rel(p)} ({len(content.splitlines())} líneas)"

    def _edit_result(self, path: str, old: str, new: str) -> tuple[Path, str, str]:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"No existe el archivo: {path}")
        text = p.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            # Modelos locales suelen fallar solo en espacios o sangría: coincidencia tolerante.
            fixed = harness.fuzzy_replace(text, old, new)
            if fixed is not None:
                return p, text, fixed
            raise ToolError("El texto a reemplazar no está en el archivo. Vuelve a leer el archivo "
                            "con read_file y copia el fragmento exacto.")
        if count > 1:
            raise ToolError(f"El texto aparece {count} veces: incluye más contexto para que sea único")
        return p, text, text.replace(old, new, 1)

    def preview_edit(self, path: str, old: str, new: str) -> str:
        _, before, after = self._edit_result(path, old, new)
        return self._diff(path, before, after)

    def edit_file(self, path: str, old: str, new: str) -> str:
        p, _, after = self._edit_result(path, old, new)
        p.write_text(after, encoding="utf-8")
        return f"Editado {self.rel(p)}"

    @staticmethod
    def _diff(path: str, old: str, new: str) -> str:
        return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                            f"a/{path}", f"b/{path}")) or "(sin cambios)"

    def sandbox_argv(self, command: str) -> list[str]:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            return ["bash", "-o", "pipefail", "-c", command]
        argv = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                "--bind", str(self.root), str(self.root), "--chdir", str(self.root),
                "--unshare-all", "--die-with-parent", "--new-session"]
        if self.allow_network:
            argv.append("--share-net")
        # El directorio personal queda oculto salvo la carpeta del proyecto
        home = Path.home()
        if home != self.root and home not in self.root.parents:
            argv[argv.index("--bind"):argv.index("--bind")] = ["--tmpfs", str(home)]
        return argv + ["bash", "-o", "pipefail", "-c", command]

    def run_command(self, command: str, timeout: int = 120) -> str:
        try:
            r = subprocess.run(self.sandbox_argv(command), cwd=self.root, capture_output=True,
                               text=True, timeout=timeout, errors="replace")
        except subprocess.TimeoutExpired:
            return f"Tiempo agotado ({timeout} s)"
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return _clip(f"[código de salida {r.returncode}]\n{out}")


BASE_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "Lista archivos y carpetas del proyecto.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Ruta relativa, por defecto '.'"},
            "depth": {"type": "integer", "description": "Profundidad (1-4)"}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Lee un archivo de texto con números de línea.",
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "search", "description": "Busca una expresión regular en los archivos del proyecto.",
        "parameters": {"type": "object", "required": ["pattern"], "properties": {
            "pattern": {"type": "string"}, "glob": {"type": "string", "description": "p. ej. '*.py'"}}}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Crea o reemplaza un archivo completo.",
        "parameters": {"type": "object", "required": ["path", "content"], "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Reemplaza un fragmento exacto y único de un archivo.",
        "parameters": {"type": "object", "required": ["path", "old", "new"], "properties": {
            "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "run_command", "description": "Ejecuta un comando bash en la carpeta del proyecto "
        "(aislado: sin red y sin acceso fuera del proyecto). Úsalo para compilar y probar.",
        "parameters": {"type": "object", "required": ["command"], "properties": {
            "command": {"type": "string"}}}}},
]


HOPE_SCHEMAS = [
    {"type": "function", "function": {
        "name": "hope", "description": "Herramientas exactas de ingeniería y ciencia (de Hope): cálculo decimal "
        "exacto, unidades, ecuaciones, derivadas, integrales, ODEs, matrices, lógica, circuitos, filtros, ajuste "
        "de curvas, constantes, G-code, STL, Marlin, materiales, binarios, hash, SQL, gráficos… Úsalas en vez de "
        "calcular de memoria. Consulta los nombres y el formato con hope_ayuda.",
        "parameters": {"type": "object", "required": ["nombre", "entrada"], "properties": {
            "nombre": {"type": "string", "description": "p. ej. calc, unidades, resolver, circuito"},
            "entrada": {"type": "string", "description": "p. ej. '11 V / (0,05 A) en ohm'"}}}}},
    {"type": "function", "function": {
        "name": "hope_ayuda", "description": "Lista las herramientas de Hope relacionadas con un tema y su formato.",
        "parameters": {"type": "object", "properties": {"tema": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "consejo", "description": "Resuelve un problema de cálculo/ingeniería con el consejo de Hope: "
        "4 agentes (innovador, conservador, optimizado, veloz) resuelven por separado y un verificador "
        "(crítico de aritmética, unidades e invariantes + consenso) elige la respuesta verificada.",
        "parameters": {"type": "object", "required": ["problema"], "properties": {
            "problema": {"type": "string"}, "tema": {"type": "string"}}}}},
]

from .docs import SCHEMAS as DOC_SCHEMAS  # noqa: E402

TOOL_SCHEMAS = BASE_SCHEMAS + [harness.VERIFY_SCHEMA, harness.TODO_SCHEMA] + HOPE_SCHEMAS + DOC_SCHEMAS
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}


def execute(ws: Workspace, name: str, args: dict) -> str:
    if name == "list_dir":
        return ws.list_dir(args.get("path", "."), max(1, min(int(args.get("depth", 2)), 4)))
    if name == "read_file":
        return ws.read_file(args["path"], int(args.get("offset", 1)), int(args.get("limit", 400)))
    if name == "search":
        return ws.search(args["pattern"], args.get("glob", "*"))
    if name == "write_file":
        return ws.write_file(args["path"], args["content"])
    if name == "edit_file":
        return ws.edit_file(args["path"], args["old"], args["new"])
    if name == "run_command":
        return ws.run_command(args["command"])
    if name == "verify":
        checks = harness.detect_checks(ws.root)
        if not checks:
            return ("No se detectaron tests ni compilación en el proyecto. "
                    "Usa run_command con el comando adecuado.")
        return "\n\n".join(f"$ {c}\n{ws.run_command(c, timeout=300)}" for c in checks)
    raise ToolError(f"Herramienta desconocida: {name}")


def preview(ws: Workspace, name: str, args: dict) -> str:
    """Texto que se muestra al usuario al pedir aprobación."""
    if name == "write_file":
        return ws.preview_write(args["path"], args["content"])
    if name == "edit_file":
        return ws.preview_edit(args["path"], args["old"], args["new"])
    if name == "run_command":
        net = "con red" if ws.allow_network else "sin red"
        return f"$ {args['command']}\n(aislado con bubblewrap, {net})"
    return ""
