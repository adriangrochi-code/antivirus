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

import os
from pathlib import Path

from .base import ToolError, _clip, safe_dir

PLANTILLAS = Path(__file__).with_name("plantillas")
NUM = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)", re.M)

EXT = {"openscad": (".scad",), "malla_info": (".stl", ".obj", ".3mf", ".ply", ".off"), "gmsh": (".geo",),
       "ngspice": (".cir", ".sp", ".spice", ".net"), "kicad_erc": (".kicad_sch",), "kicad_drc": (".kicad_pcb",),
       "octave": (".m",), "verilog": (".v", ".sv"), "yosys": (".v", ".sv"), "freecad": (".py", ".FCMacro"),
       "blender": (".py",), "pandoc": (".md",), "openfoam": ("",)}
BIN = {"openscad": "openscad", "gmsh": "gmsh", "ngspice": "ngspice", "kicad_erc": "kicad-cli",
       "kicad_drc": "kicad-cli", "octave": "octave-cli", "verilog": "iverilog", "yosys": "yosys",
       "freecad": "freecadcmd", "blender": "blender", "pandoc": "pandoc", "openfoam": "blockMesh",
       "malla_info": "python3"}
SALIDA_EXT = {"openscad": (".stl", ".3mf", ".off", ".amf"), "gmsh": (".msh",),
              "pandoc": (".pdf", ".docx", ".html", ".odt")}
TIMEOUT = {"openfoam": 1800, "blender": 900, "freecad": 600, "gmsh": 600}
MALLA_PY = r'''
import json, struct, sys
from collections import Counter
p = sys.argv[1]
try:
    import trimesh                                   # OBJ, 3MF, PLY… y STL
    m = trimesh.load(p, force="mesh")
    tris = [tuple(map(tuple, t)) for t in m.triangles.tolist()]
except ImportError:                                  # sin trimesh: STL (binario o ASCII) a mano
    data = open(p, "rb").read()
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    if len(data) == 84 + 50 * n:
        tris = [tuple(struct.unpack_from("<9f", data, 84 + 50 * i + 12)[j:j + 3] for j in (0, 3, 6)) for i in range(n)]
    else:
        v = [tuple(map(float, l.split()[1:4])) for l in data.decode(errors="replace").splitlines()
             if l.strip().startswith("vertex")]
        tris = [tuple(v[i:i + 3]) for i in range(0, len(v) - 2, 3)]
r = lambda x: round(x, 4)
key = lambda q: tuple(r(c) for c in q)
edges = Counter()
vol = area = 0.0
cx = cy = cz = 0.0
for a, b, c in tris:
    for e in ((a, b), (b, c), (c, a)):
        edges[tuple(sorted((key(e[0]), key(e[1]))))] += 1
    d = (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
    vol += d
    cx += d * (a[0] + b[0] + c[0]) / 4; cy += d * (a[1] + b[1] + c[1]) / 4; cz += d * (a[2] + b[2] + c[2]) / 4
    u = [b[i] - a[i] for i in range(3)]; w = [c[i] - a[i] for i in range(3)]
    x = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
    area += (x[0] ** 2 + x[1] ** 2 + x[2] ** 2) ** 0.5 / 2
closed = bool(tris) and all(k == 2 for k in edges.values())
pts = [q for t in tris for q in t] or [(0, 0, 0)]
print(json.dumps({"triangulos": len(tris), "estanca": closed,
    "volumen_mm3": round(abs(vol), 3) if closed else None, "area_mm2": round(area, 3),
    "caja_min": [round(min(q[i] for q in pts), 3) for i in range(3)],
    "caja_max": [round(max(q[i] for q in pts), 3) for i in range(3)],
    "centro_masa": [round(c / vol, 3) for c in (cx, cy, cz)] if closed and vol else None}))
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
        return (f"rm -f .tisma/erc.json; kicad-cli sch erc --format json --severity-all -o .tisma/erc.json {q(f)}; "
                "cat .tisma/erc.json")
    if accion == "kicad_drc":
        return (f"rm -f .tisma/drc.json; kicad-cli pcb drc --format json --severity-all -o .tisma/drc.json {q(f)}; "
                "cat .tisma/drc.json")
    if accion == "octave":
        return f"octave-cli --no-gui --quiet --norc {q(f)}"
    if accion == "verilog":
        top = opciones.get("tb") or f             # ya validado como ruta del proyecto en run()
        inc = sorted({os.path.dirname(top) or ".", os.path.dirname(f) or "."})
        return (f"iverilog -g2012 {' '.join('-I ' + q(d) for d in inc)} -o .tisma/sim.vvp {q(top)} && "
                "vvp -n .tisma/sim.vvp")
    if accion == "yosys":
        top = str(opciones.get("top", ""))
        if top and not re.fullmatch(r"\w+", top):
            raise ToolError("top: nombre de módulo inválido")
        synth = f"synth -top {top}" if top else "synth -auto-top"
        # el archivo va como argumento (no dentro del script de Yosys: un nombre con «;» o «!» inyectaría órdenes)
        return f"yosys -q -p {q(f'{synth}; tee -q -o /dev/stdout stat')} -- {q(f)}"   # -q calla también stat
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
        env = "/usr/share/openfoam/etc/bashrc"         # Debian: define WM_PROJECT_DIR (su carpeta etc)
        return (f"cd {q(f)} && {{ [ -f {env} ] && . {env} >/dev/null 2>&1; true; }} && "
                "if blockMesh > log.blockMesh 2>&1; then checkMesh > log.checkMesh 2>&1; tail -n 25 log.checkMesh; "
                f"{run} > log.solver 2>&1; tail -n 40 log.solver; "
                "else echo 'blockMesh falló:'; tail -n 30 log.blockMesh; exit 1; fi")
    raise ToolError(f"Acción desconocida: {accion}")


def open_script(f: str) -> str:
    """freecadcmd -c recibe el código: se ejecuta el archivo del proyecto con exec (mismo resultado, sin GUI)."""
    return f"exec(open({f!r}, encoding='utf-8').read())"


def summarize(accion: str, out: str) -> tuple[str, dict]:
    """Lo importante primero (y las medidas como números, para comprobarlas con `esperado`); el resto recortado."""
    head, med = [], {}
    if accion in ("openscad", "malla_info"):
        m = re.search(r"\{.*\"triangulos\".*\}", out)
        if m:
            d = json.loads(m.group(0))
            med = {k: d[k] for k in ("triangulos", "estanca", "volumen_mm3", "area_mm2") if d.get(k) is not None}
            for i, ax in enumerate("xyz"):
                med[f"tam_{ax}"] = round(d["caja_max"][i] - d["caja_min"][i], 3)
            head.append(f"Malla: {d['triangulos']} triángulos, estanca={d['estanca']}, volumen={d['volumen_mm3']} mm³, "
                        f"área={d['area_mm2']} mm², caja {d['caja_min']} → {d['caja_max']}")
            if not d["estanca"]:
                head.append("AVISO: la malla no es estanca (no se puede imprimir ni calcular su volumen)")
        head += re.findall(r"^(?:ERROR|WARNING): .*$", out, re.M)[:8]
    elif accion == "gmsh":
        n = re.findall(r"(\d+) nodes (\d+) elements", out)
        if n:
            med = {"nodos": int(n[-1][0]), "elementos": int(n[-1][1])}
            head.append(f"Malla: {med['nodos']} nodos, {med['elementos']} elementos")
        head += re.findall(r"^(?:Error|Warning)\s*:.*$", out, re.M)[:8]
    elif accion in ("ngspice", "octave"):
        for k, v in NUM.findall(out):
            med[k] = float(v)
        head += [f"{k} = {v:g}" for k, v in med.items()][:30]
        head += re.findall(r"^(?:Error|error:).*$", out, re.M)[:8]
    elif accion == "verilog":
        fails = re.findall(r"^.*FALLA.*$", out, re.M)
        if fails or re.search(r"^PASA\b", out, re.M):
            med["pasa"] = not fails
            head.append("Banco de pruebas: PASA" if not fails else "Banco de pruebas: FALLA\n" + "\n".join(fails[:10]))
        head += re.findall(r"^.*(?:error|syntax error).*$", out, re.M | re.I)[:8]
    elif accion in ("kicad_erc", "kicad_drc"):
        try:
            d, _ = json.JSONDecoder().raw_decode(out[out.index("{"):])   # detrás puede venir [stderr]
            viol = [v for sh in d.get("sheets", [d]) for v in sh.get("violations", [])] + d.get("unconnected_items", [])
            med["incidencias"] = len(viol)
            head.append(f"{len(viol)} incidencia(s)")
            head += [f"- [{v.get('severity')}] {v.get('description')}" for v in viol[:15]]
        except (ValueError, KeyError):
            pass
    elif accion == "yosys":
        cells = re.findall(r"Number of cells:?\s+(\d+)", out)
        if cells:
            med["celdas"] = int(cells[-1])
        head += [ln.strip() for ln in out.splitlines() if re.search(r"Number of cells|Chip area|\$_", ln)][:20]
    elif accion == "openfoam":
        if re.search(r"^\s*Mesh OK\.", out, re.M):
            med["malla_ok"] = True
        elif re.search(r"Failed \d+ mesh checks", out):
            med["malla_ok"] = False
        t = re.findall(r"^Time = ([-+.\deE]+)", out, re.M)
        if t:
            med["tiempo_final"] = float(t[-1])
        med["termino"] = bool(re.search(r"^End\s*$", out, re.M))
        head += re.findall(r"^\s*(?:Mesh OK\.|\*\*\*.*|Failed \d+ mesh checks\.)$", out, re.M)
        head += [f"Último paso: t = {t[-1]}" if t else "El solver no avanzó", "Solver terminado: " +
                 ("sí" if med["termino"] else "NO (mira log.solver)")]
    body = _clip(out, 6000)
    return (("\n".join(head) + "\n\n--- salida ---\n" + body) if head else body), med


def check(med: dict, esperado: dict, tol: float = 0.02) -> str:
    """Compara lo medido con lo que el agente calculó ANTES: cada discrepancia sale explícita."""
    rows, bad = [], 0
    for k, want in esperado.items():
        if k not in med:
            rows.append(f"- {k}: SIN MEDIR (la herramienta no dio ese dato; medidas: {', '.join(med) or 'ninguna'})")
            bad += 1
            continue
        got = med[k]
        if isinstance(want, bool) or isinstance(got, bool):
            ok = bool(got) == bool(want)
            rows.append(f"- {k}: {'OK' if ok else 'DISCREPANCIA'} (medido {got}, esperado {want})")
        else:
            try:
                w = float(want)
            except (TypeError, ValueError):
                rows.append(f"- {k}: esperado no numérico ({want!r})")
                bad += 1
                continue
            diff = (float(got) - w) / abs(w) if w else float(got)
            ok = abs(diff) <= tol
            rows.append(f"- {k}: {'OK' if ok else 'DISCREPANCIA'} (medido {got:g}, esperado {w:g}, {diff:+.2%})")
        bad += not ok
    title = (f"COMPROBACIÓN: todo coincide (tolerancia {tol:.0%})" if not bad else
             f"COMPROBACIÓN: {bad} DISCREPANCIA(S): revisa el modelo o tu estimación antes de seguir")
    return title + "\n" + "\n".join(rows)


def plantillas() -> dict[str, str]:
    """nombre → primera línea de comentario (qué es)."""
    out = {}
    for p in sorted(PLANTILLAS.rglob("*")):
        rel = p.relative_to(PLANTILLAS)
        if p.is_dir() and (p / "system" / "controlDict").exists():
            out[str(rel)] = "caso de OpenFOAM: " + _first_comment(p / "system" / "controlDict")
        elif p.name.endswith("_tb.v"):
            continue                                            # se copia junto a su diseño
        elif p.is_file() and "system" not in rel.parts and "constant" not in rel.parts and rel.parts[-2:-1] != ("0",):
            out[str(rel)] = _first_comment(p)
    return out


def _first_comment(p: Path) -> str:
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        t = ln.strip().lstrip("/*%#-").strip()
        if t.startswith("Plantilla TISMA:"):
            return t.split(":", 1)[1].strip()
        if t.startswith("title:"):
            return "informe en Markdown (→ PDF/DOCX con pandoc)"
    return ""


def copy_template(ws, nombre: str, destino: str = "") -> str:
    disp = plantillas()
    if nombre not in disp:
        raise ToolError(f"Plantilla desconocida: {nombre}. Hay: {', '.join(disp)}")
    src = (PLANTILLAS / nombre).resolve()
    if PLANTILLAS.resolve() not in src.parents:
        raise ToolError("Plantilla fuera de la carpeta de plantillas")
    dest = ws.resolve(destino or Path(nombre).name)
    files = [src] if src.is_file() else [f for f in sorted(src.rglob("*")) if f.is_file()]
    if src.is_file() and nombre.endswith("contador.v"):
        files.append(src.with_name("contador_tb.v"))           # el diseño con su banco de pruebas
    pairs = []
    for f in files:
        t = dest if src.is_file() and f == src else (dest.parent / f.name if src.is_file() else dest / f.relative_to(src))
        t = ws.resolve(str(t))
        if t.exists():
            raise ToolError(f"Ya existe {ws.rel(t)}: no se sobrescribe (elige otro destino)")
        pairs.append((f, t))
    for f, t in pairs:
        os.makedirs(t.parent, exist_ok=True)
        if ws.resolve(str(t.parent)) != t.parent:                # ningún enlace simbólico en el camino
            raise ToolError("Ruta de destino no válida")
        with open(t, "x", encoding="utf-8") as fh:
            fh.write(f.read_text(encoding="utf-8"))
    return "Copiado:\n" + "\n".join(f"- {ws.rel(t)}" for _, t in pairs) + \
        "\nLee el comentario de cabecera: trae la comprobación a mano para pasarla en `esperado`."


def run(ws, accion: str, archivo: str, salida: str = "", opciones: dict | None = None) -> str:
    opciones = opciones or {}
    if accion not in BIN:
        raise ToolError(f"Acción desconocida: {accion}. Opciones: {', '.join(sorted(BIN))}")
    if not shutil.which(BIN[accion]):
        hint = (" (se instala con un clic en TISMA Code: menú del modelo → Componentes → Simulación)" if accion in ("openfoam",) else
                " (viene con el uso Ingeniería: Bienvenida → Tu uso → Ingeniería y diseño)")
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
    if out_rel and not out_rel.lower().endswith(SALIDA_EXT.get(accion, ("\0",))):
        raise ToolError(f"salida de {accion} debe terminar en {', '.join(SALIDA_EXT.get(accion, ()) ) or '(sin salida)'}"
                        " (nunca sobrescribe código ni otros archivos)")
    safe_dir(ws.root, ".tisma")                   # nunca un enlace plantado
    cmd = _recipe(accion, rel, out_rel, opciones)
    text, med = summarize(accion, ws.run_command(cmd, timeout=TIMEOUT.get(accion, 300)))
    esperado = opciones.get("esperado")
    if isinstance(esperado, dict) and esperado:
        tol = opciones.get("tolerancia", 0.02)
        tol = float(tol) if isinstance(tol, (int, float)) and not isinstance(tol, bool) and 0 < tol < 1 else 0.02
        text = check(med, esperado, tol) + "\n\n" + text
    elif med:
        text += "\n\n(Pasa opciones.esperado con tu estimación previa para que la herramienta la compruebe.)"
    return text


GUIDE = """Herramientas de ingeniería (acción → para qué):
- Piezas: openscad (CAD por código, ideal para piezas paramétricas simples) o SolidCraft (CAD completo);
  después malla_info para comprobar volumen, caja y que la malla sea estanca. freecad para scripts con la API
  de FreeCAD (sólidos B-rep, STEP, piezas de chapa).
- Simulación: gmsh (mallado .geo → .msh) y, con el paquete de simulación, openfoam (fluidos); FEniCSx
  (elementos finitos) y PyVista/meshio desde Python con run_command. octave para cálculo numérico.
- Electrónica: ngspice (circuitos analógicos: usa .meas para que el resultado salga como número), kicad_erc
  y kicad_drc (reglas del esquema y la placa), verilog (simulación con banco de pruebas) y yosys (síntesis).
- Informes: pandoc (Markdown → PDF/DOCX).
Plantillas que funcionan (empieza modificando una, no desde cero): ingenieria_plantilla.
Comprobación: pasa en opciones.esperado lo que calculaste antes, p. ej. {"volumen_mm3": 7623.5} (malla),
{"fc": 159.15} (ngspice .meas), {"pasa": true} (Verilog), {"nodos": 2000} (gmsh), {"malla_ok": true} (OpenFOAM);
tolerancia por defecto 2 % (opciones.tolerancia). Medidas: malla → triangulos, estanca, volumen_mm3, area_mm2,
tam_x/y/z; ngspice/octave → cada «nombre = valor»; yosys → celdas; kicad → incidencias.
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
        "name": "ingenieria_plantilla", "description": "Copia al proyecto una plantilla que ya funciona (pieza OpenSCAD, "
        "filtro ngspice, contador Verilog con banco de pruebas, placa gmsh, viga Octave, caso OpenFOAM, informe). "
        "Sin nombre, las lista.",
        "parameters": {"type": "object", "properties": {"nombre": {"type": "string"}, "destino": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "ingenieria_guia", "description": "Qué herramienta de ingeniería usar para cada problema y cómo "
        "verificar los resultados.", "parameters": {"type": "object", "properties": {}}}},
]
