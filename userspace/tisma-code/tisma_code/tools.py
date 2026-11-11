"""Herramientas del agente, confinadas a la carpeta del proyecto.

Las de solo lectura se ejecutan directamente; las que modifican archivos o
ejecutan comandos requieren aprobación del usuario (ver ``NEEDS_APPROVAL``).
Los comandos se ejecutan dentro de bubblewrap: el sistema es de solo lectura,
solo la carpeta del proyecto es escribible y no hay red (salvo que se permita).
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import harness

from .base import MAX_OUTPUT, ToolError, _clip  # noqa: F401 (reexportados)
SKIP_DIRS = {".git", ".tisma", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", "target", ".cache"}
NEEDS_APPROVAL = {"write_file", "edit_file", "run_command", "formatear", "verify", "jarvis", "ingenieria"}


class Workspace:
    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ToolError(f"No es una carpeta: {self.root}")
        home = Path.home().resolve()
        if self.root in (Path("/"), home) or self.root in home.parents or \
                any(self.root == home / d or (home / d) in self.root.parents
                    for d in (".ssh", ".gnupg", ".config", ".local", ".mozilla", ".cache")):
            raise ToolError("Por seguridad no se puede abrir como proyecto la carpeta personal completa, "
                            "la raíz ni carpetas de configuración: elige la carpeta del proyecto")
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
        text = p.read_text(encoding="utf-8", errors="replace").splitlines()  # resolve() ya validó el destino
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
                if p.is_symlink():
                    continue
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
        """bubblewrap: sistema de solo lectura, $HOME y /run ocultos (sin D-Bus, gpg-agent, systemd
        de usuario…), entorno limpio, .git de solo lectura (no se pueden plantar hooks ni fsmonitor),
        sin red salvo permiso, sin capacidades. Sin bubblewrap no se ejecuta nada."""
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise ToolError("bubblewrap no está instalado: por seguridad no se ejecutan comandos sin aislamiento")
        home, root = str(Path.home()), str(self.root)
        argv = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/var/tmp", "--tmpfs", home,
                "--bind", root, root]
        git = self.root / ".git"
        if git.exists() and not git.is_symlink():
            argv += ["--ro-bind", str(git), str(git)]
        argv += ["--chdir", root, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
                 "--clearenv", "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--setenv", "HOME", home,
                 "--setenv", "LANG", os.environ.get("LANG", "C.UTF-8"), "--setenv", "TERM", "dumb",
                 "--setenv", "TMPDIR", "/tmp", "--setenv", "PYTHONDONTWRITEBYTECODE", "1"]
        if self.allow_network:
            argv += ["--share-net", "--ro-bind-try", "/run/systemd/resolve", "/run/systemd/resolve"]
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

from . import devtools  # noqa: E402  (importa Workspace/ToolError ya definidos arriba)
from .craft import SCHEMAS as CRAFT_SCHEMAS  # noqa: E402
from .docs import SCHEMAS as DOC_SCHEMAS  # noqa: E402
from . import ingenieria  # noqa: E402
from .jarvis import SCHEMAS as JARVIS_SCHEMAS  # noqa: E402
from .rea import SCHEMAS as REA_SCHEMAS  # noqa: E402
from .solidcraft import SCHEMAS as SOLIDCRAFT_SCHEMAS  # noqa: E402

TOOL_SCHEMAS = (BASE_SCHEMAS + devtools.SCHEMAS + [harness.VERIFY_SCHEMA, harness.TODO_SCHEMA]
                + HOPE_SCHEMAS + DOC_SCHEMAS + REA_SCHEMAS + CRAFT_SCHEMAS + SOLIDCRAFT_SCHEMAS + ingenieria.SCHEMAS
                + JARVIS_SCHEMAS)
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}


def execute(ws: Workspace, name: str, args: dict) -> str:
    if name == "list_dir":
        return ws.list_dir(args.get("path", "."), max(1, min(int(args.get("depth", 2)), 4)))
    if name == "read_file":
        return ws.read_file(args["path"], int(args.get("offset", 1)), int(args.get("limit", 400)))
    if name == "search":
        if devtools.have("rg"):
            return devtools.rg_search(ws, args["pattern"], args.get("glob", "*"))
        return ws.search(args["pattern"], args.get("glob", "*"))
    if name == "mapa_repo":
        return devtools.repo_map(ws)
    if name == "simbolo":
        return devtools.find_symbol(ws, str(args["nombre"]))
    if name == "diagnosticos":
        rutas = [str(r) for r in args.get("rutas") or []]
        if not rutas:
            raise ToolError("Indica las rutas a revisar")
        return devtools.format_diagnostics(devtools.diagnostics(ws, rutas, bool(args.get("completo"))))
    if name == "formatear":
        p, old, new = devtools.formatted(ws, args["path"])
        if old == new:
            return "El archivo ya tenía el formato correcto."
        p.write_text(new, encoding="utf-8")
        return f"Formateado {ws.rel(p)}"
    if name == "git":
        return devtools.git(ws, str(args["subcomando"]), args.get("args") or [])
    if name == "write_file":
        return ws.write_file(args["path"], args["content"])
    if name == "edit_file":
        return ws.edit_file(args["path"], args["old"], args["new"])
    if name == "run_command":
        return ws.run_command(args["command"])
    if name == "ingenieria":
        opts = args.get("opciones") or {}
        if not isinstance(opts, dict):
            raise ToolError("opciones debe ser un objeto JSON")
        return ingenieria.run(ws, str(args["accion"]), str(args["archivo"]), str(args.get("salida") or ""), opts)
    if name == "ingenieria_guia":
        disp = ingenieria.available()
        return ingenieria.GUIDE + "\n\nInstaladas: " + (", ".join(a for a, ok in sorted(disp.items()) if ok) or "ninguna")
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
    if name == "jarvis":
        return f"OpenJarvis investigará (puede buscar en Internet):\n{args.get('pregunta', '')}"
    if name == "rea":
        return f"REA: {args.get('herramienta')} {json.dumps(args.get('argumentos') or {}, ensure_ascii=False)}"
    if name == "rea_modo":
        modo = "DINÁMICO: ejecutará y observará el programa analizado" if args.get("dinamico") else "estático"
        red = " · CON acceso a la red" if args.get("red") else " · sin red"
        return (f"REA pasará a modo {modo}{red} (aislado con bubblewrap, sin acceso a tu carpeta personal).\n"
                f"Motivo: {args.get('motivo', '—')}")
    if name == "solidcraft":
        a = args.get("argumentos") or {}
        where = a.get("path") or a.get("file") or ""
        return f"SolidCraft: {args.get('herramienta')} {where}".strip() + "\n(escribe dentro de la carpeta del proyecto)"
    if name == "craft":
        return f"Craft Anything: {args.get('herramienta')} {json.dumps(args.get('argumentos') or {}, ensure_ascii=False)}"
    if name == "craft_modo":
        modo = {"profundo": "PROFUNDO: leerá el código, archivos y fuente de las páginas del objetivo",
                "ejecucion": "EJECUCIÓN: podrá ejecutar y observar el objetivo (cada ejecución pide permiso)"
                }.get(str(args.get("modo")), "limpio (solo desde fuera)")
        red = " · CON acceso a la red" if args.get("red") else " · sin red"
        return (f"Craft Anything pasará a modo {modo}{red} (aislado con bubblewrap, sin acceso a tu carpeta "
                f"personal).\nMotivo: {args.get('motivo', '—')}")
    if name == "verify":
        checks = harness.detect_checks(ws.root)
        return ("Ejecutará el código de pruebas del proyecto (aislado, sin red):\n" +
                "\n".join(f"$ {c}" for c in checks)) if checks else "No hay comprobaciones detectadas"
    if name == "formatear":
        _, old, new = devtools.formatted(ws, args["path"])
        return ws._diff(args["path"], old, new)
    if name == "ingenieria":
        extra = " " + json.dumps(args["opciones"], ensure_ascii=False) if args.get("opciones") else ""
        return (f"{args.get('accion')}: {args.get('archivo')}{extra}\n(receta fija, aislada con bubblewrap: solo ve el "
                "proyecto y sin red; los scripts de FreeCAD, Blender u Octave son código del proyecto)")
    if name == "run_command":
        net = "con red" if ws.allow_network else "sin red"
        return f"$ {args['command']}\n(aislado con bubblewrap: solo ve el proyecto, {net}, .git de solo lectura)"
    return ""
