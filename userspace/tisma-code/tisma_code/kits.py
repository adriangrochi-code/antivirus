"""Kits de herramientas: el modelo ve solo las que la tarea necesita.

Las definiciones de las ~40 herramientas ocupan unos 4.000 tokens en CADA llamada. Con un modelo pequeño de
8-16k de ventana eso es media ventana perdida y, además, cuantas más herramientas ve, peor elige. Por eso:

* el **núcleo** (leer, buscar, editar, ejecutar, verificar, tareas, cuaderno, explorar, `herramientas`) va
  siempre;
* cada **kit** (cálculo, ingeniería, CAD, documentos, desarrollo, ingeniería inversa, investigación,
  especialistas) se activa solo: por las palabras del pedido, cuando el modelo pide `herramientas` o cuando
  llama a una herramienta de un kit inactivo (se ejecuta igual y el kit queda activo);
* los kits activos solo se acumulan durante la tarea (cambiar la lista de herramientas invalida la caché del
  prompt del motor) y se vacían al empezar otra;
* con un modelo de ventana grande (≥ 64k) se exponen todas: un modelo grande sí aprovecha verlas todas.
"""

from __future__ import annotations

import re

NUCLEO = {"list_dir", "read_file", "search", "write_file", "edit_file", "run_command", "verify", "update_todos",
          "anotar", "recordar", "explorar", "herramientas"}

KITS: dict[str, dict] = {
    "desarrollo": {"desc": "programar: mapa del repositorio, símbolos, linters, formato, git",
                   "tools": {"mapa_repo", "simbolo", "diagnosticos", "formatear", "git"},
                   "claves": r"c[oó]digo|funci[oó]n|clase|m[eé]todo|bug|error|test|prueba|compila|refactor|"
                             r"repositorio|commit|git|lint|programa|script|\.py\b|\.js\b|\.ts\b|\.rs\b|\.c\b|api"},
    "calculo": {"desc": "cálculo exacto, unidades, álgebra, numérico, estadística, lógica, bits, fechas, "
                        "constantes; con Hope, su catálogo y el consejo de 4 agentes",
                "tools": {"calcular", "unidades", "algebra", "numerico", "estadistica", "logica", "bits", "fechas",
                          "huella", "constantes", "hope", "hope_ayuda", "consejo"},
                "claves": r"calcul|cu[aá]nto|ecuaci|unidad|integral|deriva|matriz|resistenc|potencia|fuerza|"
                          r"estad[ií]st|promedio|media\b|desviaci|regresi|probabil|hexadecimal|\bhex\b|bits?\b|"
                          r"complemento a dos|ieee|sha256|\bhash|checksum|fecha|d[ií]as entre|zona horaria|"
                          r"constante|demostr|satisfac|\bz3\b|"
                          r"presi[oó]n|caudal|tensi[oó]n|voltaje|corriente|energ[ií]a|masa|densidad|\d\s*(mm|cm|kg|"
                          r"n|w|v|a|hz|pa|bar|°c|kw|mpa)\b"},
    "ingenieria": {"desc": "simular y comprobar: OpenSCAD, mallas, gmsh, OpenFOAM, ngspice, KiCad, Verilog, "
                           "Octave, Blender, pandoc; plantillas",
                   "tools": {"ingenieria", "ingenieria_plantilla", "ingenieria_guia"},
                   "claves": r"simul|malla|mesh|stl|3mf|openscad|scad|gmsh|openfoam|cfd|fluido|spice|circuito|"
                             r"filtro|kicad|pcb|placa|esquem|verilog|fpga|yosys|octave|blender|render|pieza|"
                             r"imprim|viga|estructur|informe|pdf"},
    "cad": {"desc": "CAD paramétrico 3D con SolidCraft (STEP/STL/3MF, medir volúmenes)",
            "tools": {"solidcraft_buscar", "solidcraft", "solidcraft_guia"},
            "claves": r"\bcad\b|solidcraft|step|pieza|modela|3d|boceto|extru|chafl|redonde"},
    "documentos": {"desc": "buscar, leer y guardar en tus documentos locales",
                   "tools": {"docs_buscar", "docs_leer", "docs_guardar"},
                   "claves": r"document|manual|nota|apunte|guarda|biblioteca|pdf|seg[uú]n (el|mi)"},
    "inversa": {"desc": "ingeniería inversa de binarios, apps, firmware, protocolos (REA)",
                "tools": {"rea_buscar", "rea", "rea_guia", "rea_modo"},
                "claves": r"binario|ejecutable|\.exe|\.apk|\.so\b|elf|firmware|desensambl|decompil|ingenier[ií]a "
                          r"inversa|reversing|malware|protocolo|ghidra"},
    "investigacion": {"desc": "investigación larga y recrear algo a partir de una referencia (Craft Anything, "
                              "OpenJarvis con Internet)",
                      "tools": {"craft_buscar", "craft", "craft_guia", "craft_modo", "jarvis"},
                      "claves": r"investiga|recrea|clona|igual que|como la web|referencia|compara|internet|"
                                r"busca en la web|documentaci[oó]n oficial"},
    "construir": {"desc": "diseñar o construir algo desde cero (SO, base de datos, juego, navegador, lenguaje, "
                          "emulador…): piezas en orden y guías paso a paso",
                  "tools": {"guias_construir"},
                  "claves": r"desde cero|construi|crea(r)? (mi|tu|un|una) propi|mi propi[oa]|dise[ñn]a|from scratch|"
                            r"build your own|emulador|sistema operativo|motor de|int[eé]rprete|compilador|"
                            r"base de datos|navegador|juego|videojuego"},
    "especialistas": {"desc": "modelos pequeños especializados (se cargan al usarlos)",
                      "tools": {"especialista_buscar", "especialista"},
                      "claves": r"sudoku|ajedrez|traduc|ocr|especialista|clasifica|resume este"},
}

VENTANA_GRANDE = 65536

CORTO = {"desarrollo": "git, símbolos, linters", "calculo": "cálculo exacto", "ingenieria": "simular y medir",
         "cad": "CAD 3D", "documentos": "tus documentos", "inversa": "ingeniería inversa",
         "investigacion": "investigar/recrear", "construir": "construir desde cero", "especialistas": "modelos especializados"}

SCHEMA = {"type": "function", "function": {
    "name": "herramientas", "description": "Activa un kit de herramientas que no tienes cargado. Kits: " +
    "; ".join(f"{k} ({v['desc']})" for k, v in KITS.items()) + ".",
    "parameters": {"type": "object", "required": ["kit"], "properties": {
        "kit": {"type": "string", "enum": list(KITS) + ["todos"]}}}}}


def kit_of(tool: str) -> str | None:
    return next((k for k, v in KITS.items() if tool in v["tools"]), None)


def from_text(text: str) -> set[str]:
    low = text.lower()
    return {k for k, v in KITS.items() if re.search(v["claves"], low)}


def visible(all_schemas: list[dict], active: set[str], context_tokens: int) -> list[dict]:
    if context_tokens >= VENTANA_GRANDE or "todos" in active:
        return all_schemas
    names = set(NUCLEO)
    for k in active:
        names |= KITS.get(k, {}).get("tools", set())
    return [s for s in all_schemas if s["function"]["name"] in names]


def describe(active: set[str], context_tokens: int) -> str:
    if context_tokens >= VENTANA_GRANDE:
        return ""
    off = [k for k in KITS if k not in active]
    return ("\n\n[Kits activos: " + (", ".join(sorted(active)) or "solo el núcleo") + ". Si necesitas otro (" +
            ", ".join(off) + "), actívalo con herramientas.]") if off else ""

SCHEMA_CORTO = {"type": "function", "function": {
    "name": "herramientas", "description": "Activa un kit: " + "; ".join(f"{k} ({v})" for k, v in CORTO.items()),
    "parameters": SCHEMA["function"]["parameters"]}}
