"""Herramientas de ingeniería, diseño y simulación para el agente (todas libres y locales).

Cada acción es una receta fija (argumentos conocidos, nunca una orden libre) que se ejecuta en la jaula de
TISMA Code (bubblewrap, sin red, solo la carpeta del proyecto) y devuelve un resumen con los NÚMEROS que
importan (volumen, nodos, mediciones, errores) en vez de miles de líneas: así un modelo pequeño puede iterar
y comprobar sus resultados.

Acciones:
  openscad      .scad → .stl (y comprueba la malla)      freecad      script Python de FreeCAD (freecadcmd)
  malla_info    volumen, caja, estanqueidad de STL/OBJ/3MF  gmsh         .geo → .msh (nodos, elementos)
  ngspice       simulación de un circuito (.cir) en lote    kicad_erc    reglas eléctricas de un esquema
  kicad_drc     reglas de diseño de una placa              octave       script .m
  verilog       compila y simula con Icarus (iverilog/vvp)  yosys        síntesis: celdas y área estimada
  openfoam      caso CFD: blockMesh + checkMesh + solver    blender      script Python de Blender sin ventana
  pandoc        documento Markdown → PDF/DOCX/HTML
"""

from __future__ import annotations

import json
import re
import shlex
import shutil

from .base import ToolError, _clip, safe_dir

EXT = {"openscad": (".scad",), "malla_info": (".stl", ".obj", ".3mf", ".ply", ".off"), "gmsh": (".geo",),
       "ngspice": (".cir", ".sp", ".spice", ".net"), "kicad_erc": (".kicad_sch",), "kicad_drc": (".kicad_pcb",),
       "octave": (".m",), "verilog": (".v", ".sv"), "yosys": (".v", ".sv"), "freecad": (".py", ".FCMacro"),
       "blender": (".py",), "pandoc": (".md",), "openfoam": ("",)}
BIN = {"openscad": "openscad", "gmsh": "gmsh", "ngspice": "ngspice", "kicad_erc": "kicad-cli",
       "kicad_drc": "kicad-cli", "octave": "octave-cli", "verilog": "iverilog", "yosys": "yosys",
       "freecad": "freecadcmd", "blender": "blender", "pandoc": "pandoc", "openfoam": "blockMesh",
       "malla_info": "python3"}
TIMEOUT = {"openfoam": 1800, "blender": 900, "freecad": 600, "gmsh": 600}
MALLA_PY = r'''
import json, sys
import trimesh
m = trimesh.load(sys.argv[1], force="mesh")
b = m.bounds.tolist()
print(json.dumps({"triangulos": int(len(m.faces)), "estanca": bool(m.is_watertight),
    "volumen_mm3": round(float(m.volume), 3) if m.is_watertight else None, "area_mm2": round(float(m.area), 3),
    "caja_min": [round(x, 3) for x in b[0]], "caja_max": [round(x, 3) for x in b[1]],
    "centro_masa": [round(x, 3) for x in m.center_mass] if m.is_watertight else None}))
'''


def available() -> dict[str, bool]:
    return {a: shutil.which(b) is not None for a, b in BIN.items()}


def _recipe(accion: str, f: str, salida: str, opciones: dict) -> str:
    q = shlex.quote
    if accion == "openscad":
        out = salida or re.sub(r"\.scad$", ".stl", f)
        return f"openscad -o {q(out)} --export-format binstl {q(f)} && python3 -c {q(MALLA_PY)} {q(out)}"
    if accion == "malla_info":
        return f"python3 -c {q(MALLA_PY)} {q(f)}"
    if accion == "gmsh":
        dim = int(opciones.get("dimension", 3))
        if dim not in (1, 2, 3):
            raise ToolError("dimension debe ser 1, 2 o 3")
        out = salida or re.sub(r"\.geo$", ".msh", f)
        return f"gmsh -{dim} -format msh4 -o {q(out)} {q(f)}"
    if accion == "ngspice":
        return f"ngspice -b {q(f)}"
    if accion == "kicad_erc":
        return f"kicad-cli sch erc --format json --severity-all -o .tisma/erc.json {q(f)}; cat .tisma/erc.json"
    if accion == "kicad_drc":
        return f"kicad-cli pcb drc --format json --severity-all -o .tisma/drc.json {q(f)}; cat .tisma/drc.json"
    if accion == "octave":
        return f"octave-cli --no-gui --quiet --norc {q(f)}"
    if accion == "verilog":
        top = opciones.get("tb") or f             # ya validado como ruta del proyecto en run()
        return f"iverilog -g2012 -o .tisma/sim.vvp {q(top)} && vvp -n .tisma/sim.vvp"
    if accion == "yosys":
        top = str(opciones.get("top", ""))
        if top and not re.fullmatch(r"\w+", top):
            raise ToolError("top: nombre de módulo inválido")
        synth = f"synth -top {top}" if top else "synth -auto-top"
        # el archivo va como argumento (no dentro del script de Yosys: un nombre con «;» o «!» inyectaría órdenes)
        return f"yosys -q -p {q(f'{synth}; stat')} -- {q(f)}"
    if accion == "freecad":
        return f"freecadcmd -c {q(open_script(f))}"
    if accion == "blender":
        return f"blender -b --factory-startup -noaudio --python {q(f)}"
    if accion == "pandoc":
        fmt = str(opciones.get("formato", "pdf"))
        if fmt not in ("pdf", "docx", "html", "odt"):
            raise ToolError("formato: pdf, docx, html u odt")
        out = salida or re.sub(r"\.md$", f".{fmt}", f)
        return f"pandoc {q(f)} -o {q(out)}" + (" --pdf-engine=pdflatex" if fmt == "pdf" else "")
    if accion == "openfoam":
        solver = str(opciones.get("solver", ""))
        if solver and not re.fullmatch(r"[A-Za-z]+Foam", solver):
            raise ToolError("solver: un nombre de OpenFOAM (p. ej. simpleFoam)")
        run = solver or "$(foamDictionary -entry application -value system/controlDict)"
        return (f"cd {q(f)} && blockMesh > log.blockMesh 2>&1 && "
                f"checkMesh > log.checkMesh 2>&1; tail -n 25 log.checkMesh; {run} > log.solver 2>&1; "
                "tail -n 30 log.solver")
    raise ToolError(f"Acción desconocida: {accion}")


def open_script(f: str) -> str:
    """freecadcmd -c recibe el código: se ejecuta el archivo del proyecto con exec (mismo resultado, sin GUI)."""
    return f"exec(open({f!r}, encoding='utf-8').read())"


def summarize(accion: str, out: str) -> str:
    """Lo importante primero; el resto recortado."""
    head = []
    if accion in ("openscad", "malla_info"):
        m = re.search(r"\{.*\"triangulos\".*\}", out)
        if m:
            d = json.loads(m.group(0))
            head.append(f"Malla: {d['triangulos']} triángulos, estanca={d['estanca']}, volumen={d['volumen_mm3']} mm³, "
                        f"caja {d['caja_min']} → {d['caja_max']}")
            if not d["estanca"]:
                head.append("AVISO: la malla no es estanca (no se puede imprimir ni calcular su volumen)")
        errs = re.findall(r"^(?:ERROR|WARNING): .*$", out, re.M)
        head += errs[:8]
    elif accion == "gmsh":
        n = re.search(r"(\d+) nodes (\d+) elements", out)
        if n:
            head.append(f"Malla: {n.group(1)} nodos, {n.group(2)} elementos")
        head += re.findall(r"^(?:Error|Warning)\s*:.*$", out, re.M)[:8]
    elif accion == "ngspice":
        head += [ln.strip() for ln in out.splitlines() if re.match(r"\s*\w+\s+=\s+[-+0-9.eE]+", ln)][:30]
        head += re.findall(r"^Error.*$", out, re.M)[:8]
    elif accion in ("kicad_erc", "kicad_drc"):
        try:
            d = json.loads(out[out.index("{"):])
            viol = [v for s in d.get("sheets", [d]) for v in s.get("violations", [])] + d.get("unconnected_items", [])
            head.append(f"{len(viol)} incidencia(s)")
            head += [f"- [{v.get('severity')}] {v.get('description')}" for v in viol[:15]]
        except (ValueError, KeyError):
            pass
    elif accion == "yosys":
        head += [ln.strip() for ln in out.splitlines() if re.search(r"Number of cells|Chip area|\$_", ln)][:20]
    elif accion == "openfoam":
        head += re.findall(r"^\s*(?:Mesh OK\.|\*\*\*.*|Failed \d+ mesh checks\.)$", out, re.M)
        head += re.findall(r"^(?:Time = .*|End)$", out, re.M)[-3:]
    body = _clip(out, 6000)
    return ("\n".join(head) + "\n\n--- salida ---\n" + body) if head else body


def run(ws, accion: str, archivo: str, salida: str = "", opciones: dict | None = None) -> str:
    opciones = opciones or {}
    if accion not in BIN:
        raise ToolError(f"Acción desconocida: {accion}. Opciones: {', '.join(sorted(BIN))}")
    if not shutil.which(BIN[accion]):
        hint = " (instálalo con: sudo centinela-apps simulacion)" if accion in ("openfoam",) else ""
        raise ToolError(f"{BIN[accion]} no está instalado{hint}")
    p = ws.resolve(archivo)                       # dentro del proyecto (resolución segura de rutas)
    if EXT[accion][0] and not str(p).endswith(EXT[accion]):
        raise ToolError(f"{accion} espera un archivo {', '.join(EXT[accion])}")
    if accion == "openfoam" and not (p.is_dir() and (p / "system" / "controlDict").exists()):
        raise ToolError("openfoam espera la carpeta de un caso (con system/controlDict)")
    rel = ws.rel(p)
    if accion == "verilog" and opciones.get("tb"):
        tb = ws.resolve(str(opciones["tb"]))
        if not str(tb).endswith(EXT["verilog"]):
            raise ToolError("tb: un archivo .v o .sv del proyecto")
        opciones = {**opciones, "tb": ws.rel(tb)}
    out_rel = ws.rel(ws.resolve(salida)) if salida else ""
    safe_dir(ws.root, ".tisma")                   # nunca un enlace plantado
    cmd = _recipe(accion, rel, out_rel, opciones)
    return summarize(accion, ws.run_command(cmd, timeout=TIMEOUT.get(accion, 300)))


GUIDE = """Herramientas de ingeniería (acción → para qué):
- Piezas: openscad (CAD por código, ideal para piezas paramétricas simples) o SolidCraft (CAD completo);
  después malla_info para comprobar volumen, caja y que la malla sea estanca. freecad para scripts con la API
  de FreeCAD (sólidos B-rep, STEP, piezas de chapa).
- Simulación: gmsh (mallado .geo → .msh) y, con el paquete de simulación, openfoam (fluidos); FEniCSx
  (elementos finitos) y PyVista/meshio desde Python con run_command. octave para cálculo numérico.
- Electrónica: ngspice (circuitos analógicos: usa .meas para que el resultado salga como número), kicad_erc
  y kicad_drc (reglas del esquema y la placa), verilog (simulación con banco de pruebas) y yosys (síntesis).
- Informes: pandoc (Markdown → PDF/DOCX).
Rigor: antes de simular, estima a mano el orden de magnitud del resultado (con hope si hace falta); si la
simulación da algo muy distinto, revisa el modelo antes de darlo por bueno. Cita siempre los números de la
herramienta, no los inventes."""

SCHEMAS = [
    {"type": "function", "function": {
        "name": "ingenieria", "description": "Ejecuta una herramienta de ingeniería sobre un archivo del proyecto y "
        "devuelve los números clave: openscad, malla_info, gmsh, ngspice, kicad_erc, kicad_drc, octave, verilog, "
        "yosys, freecad, blender, pandoc, openfoam. Ver ingenieria_guia.",
        "parameters": {"type": "object", "required": ["accion", "archivo"], "properties": {
            "accion": {"type": "string", "enum": sorted(BIN)}, "archivo": {"type": "string"},
            "salida": {"type": "string"}, "opciones": {"type": "object"}}}}},
    {"type": "function", "function": {
        "name": "ingenieria_guia", "description": "Qué herramienta de ingeniería usar para cada problema y cómo "
        "verificar los resultados.", "parameters": {"type": "object", "properties": {}}}},
]
