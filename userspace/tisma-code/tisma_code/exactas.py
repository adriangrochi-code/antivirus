"""Herramientas de cálculo exacto propias de TISMA Code (respaldo mientras Hope no vaya en la ISO pública).

Cuando Hope sea open source y venga en la imagen, su catálogo (más amplio y certificado) las reemplaza. Hasta
entonces, esto evita que el modelo calcule «de memoria» en la ISO pública.

Todo se ejecuta en exactas_worker.py dentro de bubblewrap: sin red, sistema de solo lectura, el proyecto solo
de lectura (para leer CSV o calcular huellas de archivos), $HOME oculto, sin capacidades, con límite de memoria,
CPU y tiempo. Sin bubblewrap no se ejecuta (falla cerrado).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

WORKER = Path(__file__).with_name("exactas_worker.py")
TIEMPO = 60

_S = "string"
SCHEMAS = [
    {"type": "function", "function": {
        "name": "calcular", "description": "Calcula EXACTO (racionales: 0.1+0.2 == 0.3 es verdadero; enteros "
        "enormes) o con las cifras que pidas para irracionales. Admite + - * / ^ % // paréntesis, comparaciones "
        "(== < >) para COMPROBAR un resultado, operaciones de bits, sqrt, cbrt, exp, ln, log10, sin/cos/tan (en "
        "radianes), deg, rad, factorial, comb, perm, mcd, mcm, primo, factores, redondear. Úsala SIEMPRE en vez de "
        "calcular de memoria.",
        "parameters": {"type": "object", "required": ["expresion"], "properties": {
            "expresion": {"type": _S, "description": "p. ej. '(3/7 + 2^10) * 1,5' o 'sqrt(2)^2 == 2'"},
            "digitos": {"type": "integer", "description": "cifras para resultados irracionales (5-1000, 30 por defecto)"}}}}},
    {"type": "function", "function": {
        "name": "unidades", "description": "Cálculo con unidades y comprobación de dimensiones (pint): convierte y "
        "avisa si una fórmula mezcla magnitudes incompatibles.",
        "parameters": {"type": "object", "required": ["expresion"], "properties": {
            "expresion": {"type": _S, "description": "p. ej. '11 V / (0.05 A)' o '60 mph'"},
            "a": {"type": _S, "description": "unidad de destino, p. ej. 'ohm', 'km/h', 'kW'"}}}}},
    {"type": "function", "function": {
        "name": "algebra", "description": "Álgebra simbólica exacta (sympy): simplificar, factorizar, expandir, "
        "resolver (ecuación con '='), sistema (ecuaciones separadas por ';'), derivar, integrar (con limites para "
        "definida), limite, serie, evaluar, matriz (det, inversa, autovalores, rango), edo (ecuación diferencial "
        "con y', y'' y condiciones).",
        "parameters": {"type": "object", "required": ["operacion", "expresion"], "properties": {
            "operacion": {"type": _S, "enum": ["simplificar", "factorizar", "expandir", "resolver", "sistema",
                                                "derivar", "integrar", "limite", "serie", "evaluar", "matriz", "edo"]},
            "expresion": {"type": _S, "description": "p. ej. 'x^2 - 5x + 6 = 0', '2x+y=5; x-y=1', '1 2; 3 4', "
                                                       "\"y'' + y = 0\""},
            "variable": {"type": _S, "description": "variable independiente (x por defecto)"},
            "limites": {"type": "array", "items": {"type": _S}, "description": "[a, b] para integral definida"},
            "punto": {"type": _S}, "orden": {"type": "integer"},
            "valores": {"type": "object", "description": "para evaluar: {\"x\": \"2\"}"},
            "condiciones": {"type": _S, "description": "para edo: \"y(0)=1; y'(0)=0\""},
            "funcion": {"type": _S, "description": "para edo: nombre de la función (y por defecto)"}}}}},
    {"type": "function", "function": {
        "name": "numerico", "description": "Métodos numéricos con garantía o estimación de error: raiz (bisección "
        "garantizada en un intervalo con cambio de signo), integral (con error estimado), minimizar, edo (sistema "
        "dy/dt=…, RK45 con tolerancia 1e-10), ajuste (parámetros de un modelo con incertidumbre y R²).",
        "parameters": {"type": "object", "required": ["operacion", "expresion"], "properties": {
            "operacion": {"type": _S, "enum": ["raiz", "integral", "minimizar", "edo", "ajuste"]},
            "expresion": {"type": _S, "description": "p. ej. 'cos(x) - x', 'dx/dt = v; dv/dt = -x', 'a*exp(b*x)'"},
            "variable": {"type": _S}, "variables": {"type": "array", "items": {"type": _S}},
            "intervalo": {"type": "array", "items": {"type": "number"}},
            "inicio": {"type": "array", "items": {"type": "number"}},
            "parametros": {"type": "array", "items": {"type": _S}},
            "x": {"type": "array", "items": {"type": "number"}}, "y": {"type": "array", "items": {"type": "number"}},
            "puntos": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "estadistica", "description": "Estadística: describir (media, mediana, desviación, cuartiles, IC 95 %), "
        "regresion, correlacion, prueba_t. Datos en lista o columna de un CSV del proyecto.",
        "parameters": {"type": "object", "properties": {
            "operacion": {"type": _S, "enum": ["describir", "regresion", "correlacion", "prueba_t"]},
            "datos": {"type": "array", "items": {"type": "number"}},
            "datos_b": {"type": "array", "items": {"type": "number"}},
            "x": {"type": "array", "items": {"type": "number"}}, "y": {"type": "array", "items": {"type": "number"}},
            "archivo": {"type": _S, "description": "CSV del proyecto"}, "columna": {"type": _S}}}}},
    {"type": "function", "function": {
        "name": "logica", "description": "Demuestra o encuentra contraejemplos con el solver SMT z3 (SMT-LIB2). Para "
        "DEMOSTRAR una propiedad, afirma su negación: si sale unsat, la propiedad es cierta.",
        "parameters": {"type": "object", "required": ["smtlib"], "properties": {
            "smtlib": {"type": _S, "description": "p. ej. '(declare-const x Int) (assert (> (* x x) 10)) (assert (< x 4))'"}}}}},
    {"type": "function", "function": {
        "name": "bits", "description": "Bases y bits: convertir (dec/hex/bin/oct), complemento a dos con un ancho, "
        "bytes big/little-endian, ieee754 (float32/float64: codificar o decodificar 0x…).",
        "parameters": {"type": "object", "required": ["valor"], "properties": {
            "operacion": {"type": _S, "enum": ["convertir", "ieee754"]}, "valor": {"type": _S},
            "ancho": {"type": "integer"}, "tipo": {"type": _S, "enum": ["float32", "float64"]}}}}},
    {"type": "function", "function": {
        "name": "fechas", "description": "Fechas exactas: diferencia (días, semanas, laborables), sumar una duración "
        "('1 mes 3 dias'), dia (día de la semana, semana ISO), zona (convertir entre zonas horarias IANA).",
        "parameters": {"type": "object", "required": ["operacion", "desde"], "properties": {
            "operacion": {"type": _S, "enum": ["diferencia", "sumar", "dia", "zona"]},
            "desde": {"type": _S, "description": "AAAA-MM-DD [HH:MM], DD/MM/AAAA o 'hoy'"}, "hasta": {"type": _S},
            "duracion": {"type": _S}, "zona_origen": {"type": _S}, "zona_destino": {"type": _S}}}}},
    {"type": "function", "function": {
        "name": "huella", "description": "Huella de un texto o archivo del proyecto: sha256, sha512, sha1, md5, "
        "blake2b, crc32, adler32.",
        "parameters": {"type": "object", "properties": {
            "texto": {"type": _S}, "archivo": {"type": _S}, "algoritmo": {"type": _S}}}}},
    {"type": "function", "function": {
        "name": "constantes", "description": "Constantes físicas oficiales CODATA con su incertidumbre (Planck, "
        "Boltzmann, masa del electrón, G…).",
        "parameters": {"type": "object", "required": ["nombre"], "properties": {"nombre": {"type": _S}}}}},
]
NOMBRES = {s["function"]["name"] for s in SCHEMAS}


class ExactaError(Exception):
    pass


def _limites():
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (3 << 30, 3 << 30))       # numpy/scipy reservan memoria virtual
    resource.setrlimit(resource.RLIMIT_CPU, (TIEMPO, TIEMPO))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))


def argv(raiz: Path) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise ExactaError("bubblewrap no está instalado: por seguridad las herramientas de cálculo no se ejecutan")
    home, r = str(Path.home()), str(Path(raiz).resolve())
    return [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/run",
            "--tmpfs", "/var/tmp", "--tmpfs", home, "--ro-bind", r, r, "--chdir", r,
            "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", home, "--setenv", "LANG", "C.UTF-8",
            "--setenv", "MPLCONFIGDIR", "/tmp", "--setenv", "OMP_NUM_THREADS", "2",
            "--", shutil.which("python3") or "/usr/bin/python3", "-I", str(WORKER)]


def run(raiz: Path, nombre: str, args: dict) -> tuple[str, bool]:
    """(resultado, ok). Las rutas de archivo deben estar dentro del proyecto."""
    if nombre not in NOMBRES:
        raise ExactaError(f"herramienta desconocida: {nombre}")
    raiz = Path(raiz).resolve()
    if args.get("archivo"):
        p = (raiz / str(args["archivo"])).resolve()
        if raiz != p and raiz not in p.parents:
            return "El archivo debe estar dentro del proyecto", False
        args = {**args, "archivo": str(p)}
    pedido = json.dumps({"herramienta": nombre, "args": args}, ensure_ascii=False)
    try:
        r = subprocess.run(argv(raiz), input=pedido, capture_output=True, text=True, timeout=TIEMPO + 10,
                           preexec_fn=_limites, cwd=str(raiz), env={"PATH": os.environ.get("PATH", "/usr/bin")})
    except subprocess.TimeoutExpired:
        return f"Tiempo agotado ({TIEMPO} s): simplifica el problema o pide menos cifras", False
    try:
        res = json.loads(r.stdout)
    except ValueError:
        err = (r.stderr or "").strip().splitlines()[-1:] or ["sin salida"]
        return f"El cálculo falló (probablemente sin memoria o tiempo): {err[0][:300]}", False
    return str(res.get("resultado", "")), bool(res.get("ok"))
