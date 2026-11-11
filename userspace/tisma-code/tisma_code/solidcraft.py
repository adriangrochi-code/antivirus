"""SolidCraft (CAD paramétrico 3D, MIT/Apache-2.0, github.com/tangency-labs/solidcraft) como herramienta de TISMA Code.

El agente modela piezas con los mismos comandos que una persona (bocetos con restricciones, operaciones,
parámetros, STEP/STL/3MF) y, sobre todo, puede VERIFICAR cada paso con números: calcula antes el volumen,
el número de cuerpos o la caja envolvente que debe dar y se lo pasa a la llamada (`expect`); el servidor mide
el diseño y marca error si no coincide. Encaja con el rigor del arnés: nada se da por bueno sin medirlo.

Aislamiento: `solidcraft-cli mcp` (sesión sin ventana) en bubblewrap, sin red, con $HOME oculto y acceso a
archivos solo en la carpeta del proyecto (además de su propio límite --allow-path). Modelar no pide permiso
(el diseño está en memoria y se deshace); guardar, exportar y empezar un diseño nuevo sí.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .mcp_client import MCPClient, MCPError

ASK = {"save", "export", "new_design"}
GUIDE = """Método para modelar con SolidCraft (rigor: cada etapa se comprueba con números):
1. Antes de construir, calcula a mano (o con hope) lo que la etapa debe producir: volumen en mm³, número de
   cuerpos y caja envolvente. Pásalo como `expect` en `execute` o `batch`; si no coincide, la llamada da
   error con la diferencia y decides si deshacer (undo) o corregir la operación.
2. Usa parámetros con nombre (set_parameter) para las cotas; prueba variantes con check_parameters (no
   modifica el diseño) en vez de cambiar y deshacer.
3. Agrupa los pasos de una etapa en `batch` (se deshace entera si un paso falla).
4. Comprueba con `measure` (summary: true) y `inspect_design` (timeline: "summary"); localiza agujeros y
   ejes con body_topology(faces: "cylinders").
5. Usa list_commands(filter=…, compact: true) y describe_command para conocer los parámetros exactos de un
   comando: no los inventes.
6. Guarda (.sc) y exporta (STEP para CAD, 3MF/STL para imprimir) dentro del proyecto; piden permiso."""


def solidcraft_command() -> list[str] | None:
    if os.environ.get("TISMA_SOLIDCRAFT_CMD"):
        return os.environ["TISMA_SOLIDCRAFT_CMD"].split()
    exe = shutil.which("solidcraft-cli")
    return [exe] if exe else None


class SolidCraftTools:
    def __init__(self, ws, command: list[str] | None = None, jail: bool = True):
        self.ws = ws
        cmd = command or solidcraft_command() or []
        if not cmd:
            raise MCPError("SolidCraft no está instalado (Tienda TISMA o «centinela-apps solidcraft»)")
        argv = cmd + ["mcp", "--headless", "--allow-path", str(ws.root)]
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp/solidcraft-home", "LANG": "C.UTF-8",
               "XDG_RUNTIME_DIR": "/tmp/solidcraft-run"}
        if jail:
            argv = self._jail(argv)
        self.client = MCPClient(argv, env=env, cwd=str(ws.root), timeout=600)
        self.tools = {t["name"]: t for t in self.client.list_tools()}

    def _jail(self, argv: list[str]) -> list[str]:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise MCPError("bubblewrap no está instalado: SolidCraft no se ejecuta sin aislamiento")
        root, home = str(self.ws.root), str(Path.home())
        # /dev entero: la GPU (/dev/dri) acelera las capturas sin ventana; sin red ni carpeta personal
        return [bwrap, "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                "--tmpfs", "/run", "--tmpfs", home, "--bind", root, root, "--chdir", root,
                "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--", *argv]

    def needs_approval(self, name: str) -> bool:
        return name in ASK or name not in self.tools

    def search(self, query: str, limit: int = 15) -> str:
        words = re.findall(r"\w{3,}", query.lower())
        rows = []
        for n, t in self.tools.items():
            text = (n + " " + (t.get("description") or "")).lower()
            score = sum(w in text for w in words) if words else 1
            if score:
                rows.append((score, n, t))
        rows.sort(key=lambda r: (-r[0], r[1]))
        out = [f"- {n}{' (pide permiso)' if n in ASK else ''}: {(t.get('description') or '').strip()[:220]}"
               for _, n, t in rows[:limit]]
        return ("\n".join(out) or "Sin herramientas para esa búsqueda") + \
            "\nLos comandos de modelado se listan con la herramienta list_commands (filter, compact: true)."

    def call(self, name: str, arguments: dict) -> str:
        if name not in self.tools:
            raise MCPError(f"SolidCraft no tiene la herramienta «{name}» (usa solidcraft_buscar)")
        return self.client.call_tool(name, arguments)

    def close(self) -> None:
        self.client.close()


def guide(topic: str = "") -> str:
    return GUIDE


SCHEMAS = [
    {"type": "function", "function": {
        "name": "solidcraft_buscar", "description": "Herramientas de SolidCraft (CAD paramétrico 3D): modelar piezas, "
        "medir volúmenes y cotas, exportar STEP/STL/3MF.",
        "parameters": {"type": "object", "required": ["consulta"], "properties": {"consulta": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "solidcraft", "description": "Ejecuta una herramienta de SolidCraft (execute, batch, measure, "
        "inspect_design, set_parameter, check_parameters, list_commands, describe_command, screenshot, save, export…). "
        "En execute/batch pasa `expect` con el volumen/caja esperados para verificar cada etapa.",
        "parameters": {"type": "object", "required": ["herramienta"], "properties": {
            "herramienta": {"type": "string"}, "argumentos": {"type": "object"}}}}},
    {"type": "function", "function": {
        "name": "solidcraft_guia", "description": "Método para modelar piezas con SolidCraft verificando cada etapa "
        "con números (léelo antes de modelar).",
        "parameters": {"type": "object", "properties": {"tema": {"type": "string"}}}}},
]
