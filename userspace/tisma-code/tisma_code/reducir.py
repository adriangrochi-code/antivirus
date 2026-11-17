"""«✂ Reducir»: un modelo descargado más chico y más rápido, quitando lo que menos aporta, con la calidad MEDIDA.

Poner pesos en cero no achica un GGUF (los ceros ocupan lo mismo). Lo que sí reduce tamaño y acelera:
1. Guardar con menos precisión los pesos menos importantes y mantener precisos los importantes: llama-imatrix
   mide la importancia con texto de muestra y llama-quantize la usa (--imatrix).
2. (Nivel «máximo») Quitar CAPAS enteras que casi no cambian el resultado: se prueban candidatas una a una
   (llama-quantize --prune-layers) midiendo la perplejidad con llama-perplexity, y solo se quitan las que menos
   la empeoran, con un tope de pérdida.
Inspirado en la Lottery Ticket Hypothesis (Frankle y Carbin, 2019: dentro de la red hay una subred «ganadora»
que rinde casi igual). La versión original re-entrena varias veces para encontrarla: imposible en un portátil, y
la poda suelta no acelera llama.cpp. Aquí se busca la subred quitando piezas ENTERAS (capas) y midiendo, sin
re-entrenar; el ajuste fino posterior («curar» la red podada) queda para un equipo con GPU o la nube escolar.
El original NUNCA se borra: el reducido queda como un modelo más. Las herramientas leen archivos descargados de
internet: corren en bubblewrap sin red, sin la carpeta personal y sin capacidades (como el motor).
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from . import models

# Tipo de destino según la precisión de origen (de «general.file_type» o del nombre del archivo)
DESTINO = {"f32": "Q4_K_M", "f16": "Q4_K_M", "bf16": "Q4_K_M", "q8_0": "Q4_K_M", "q6_k": "Q4_K_M",
           "q5_k_m": "Q4_K_M", "q5_k_s": "Q4_K_M", "q5_0": "Q4_K_M", "q4_k_m": "IQ3_M", "q4_k_s": "IQ3_M",
           "q4_0": "IQ3_M", "iq4_xs": "IQ3_M", "iq4_nl": "IQ3_M", "q3_k_m": "IQ2_M", "iq3_m": "IQ2_M"}
FILE_TYPE = {0: "f32", 1: "f16", 7: "q8_0", 2: "q4_0", 15: "q4_k_m", 14: "q4_k_s", 17: "q5_k_m", 16: "q5_k_s",
             18: "q6_k", 12: "q3_k_m", 30: "iq4_xs", 25: "iq4_nl", 27: "iq3_m", 32: "bf16", 8: "q5_0"}
# Lo que se puede elegir («esfuerzo»), de más a menos precisión. Bits por peso aproximados, para no ofrecer
# «subir» la precisión (no recupera lo perdido) ni repetir la misma.
NIVELES = {"normal": ("Q4_K_M", 4.9), "liviano": ("IQ3_M", 3.7), "muy_liviano": ("IQ2_M", 2.7)}
BITS = {"f32": 32, "f16": 16, "bf16": 16, "q8_0": 8.5, "q6_k": 6.6, "q5_k_m": 5.7, "q5_k_s": 5.5, "q5_0": 5.5,
        "q4_k_m": 4.9, "q4_k_s": 4.6, "q4_0": 4.5, "iq4_xs": 4.3, "iq4_nl": 4.5, "q3_k_m": 3.9, "iq3_m": 3.7}
# Elección manual («Avanzado»): formato exacto de llama-quantize → (bits por peso aprox., descripción simple).
# De más a menos precisión. Los IQ (más chicos a igual calidad) siempre con la importancia medida.
MANUALES = {
    "Q8_0": (8.5, "casi sin pérdida, el doble de grande que Q4"),
    "Q6_K": (6.6, "casi sin pérdida"),
    "Q5_K_M": (5.7, "muy poca pérdida"),
    "Q4_K_M": (4.9, "el equilibrio habitual"),
    "IQ4_XS": (4.3, "como Q4, algo más chico"),
    "Q3_K_M": (3.9, "pierde algo de calidad"),
    "IQ3_M": (3.7, "pierde algo; buen tamaño para equipos modestos"),
    "IQ3_XXS": (3.1, "pierde bastante"),
    "IQ2_M": (2.7, "pierde mucho: solo equipos muy justos"),
    "Q2_K": (2.6, "pierde mucho"),
}
PERDIDA_MAX_CAPA = 0.04        # cada capa quitada puede empeorar la perplejidad como mucho un 4 %
PERDIDA_MAX_TOTAL = 0.12       # y todas juntas, un 12 %
MAX_CANDIDATAS = 6
_PPL = re.compile(r"Final estimate:\s*PPL\s*=\s*([\d.]+)")


class ReducirError(RuntimeError):
    pass


def precision(path: Path, file_type: int | None = None) -> str | None:
    if file_type in FILE_TYPE:
        return FILE_TYPE[file_type]
    m = re.search(r"(?i)[-_.](f32|f16|bf16|q8_0|q6_k|q5_k_m|q5_k_s|q5_0|q4_k_m|q4_k_s|q4_0|iq4_xs|iq4_nl|q3_k_m|iq3_m)"
                  r"(?=[-_.])", path.name)
    return m.group(1).lower() if m else None


def niveles_posibles(origen: str | None) -> list[str]:
    """Los niveles que de verdad achican este modelo (menos bits que los que ya tiene)."""
    bits = BITS.get(origen or "")
    return [k for k, (_, b) in NIVELES.items() if bits and b < bits - 0.15]


def manuales_posibles(origen: str | None) -> list[str]:
    """Formatos que achican este modelo (no se ofrece «subir» la precisión: no recupera lo perdido)."""
    bits = BITS.get(origen or "")
    return [t for t, (b, _) in MANUALES.items() if bits and b < bits - 0.15]


def objetivo_calidad(nivel: str) -> int | None:
    """«calidad:90» → 90 (lo mínimo que se acepta conservar, en %). Fuera de 50–99 no vale."""
    m = re.fullmatch(r"calidad:(\d{1,3})", nivel or "")
    if not m:
        return None
    v = int(m.group(1))
    if not 50 <= v <= 99:
        raise ReducirError("La calidad mínima tiene que estar entre 50 % y 99 %.")
    return v


def buscar_por_calidad(tipos: list[str], medir, objetivo: float) -> tuple[str | None, dict[str, float]]:
    """El formato MÁS CHICO que conserva al menos `objetivo` % de calidad, midiendo lo menos posible.

    tipos: de más a menos bits; medir(tipo) → % de calidad medido. La calidad baja (casi siempre) con los bits, así
    que se busca por mitades: con 10 formatos, unas 4 mediciones en vez de 10. Devuelve (tipo o None si ninguno
    llega, {tipo: calidad medida})."""
    medidas: dict[str, float] = {}
    lo, hi, mejor = 0, len(tipos) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        q = medidas[tipos[mid]] = medir(tipos[mid])
        if q >= objetivo:
            mejor, lo = tipos[mid], mid + 1          # llega: probar uno más chico
        else:
            hi = mid - 1                             # no llega: probar uno más grande
    return mejor, medidas


def tipo_destino(origen: str | None, nivel: str) -> str:
    if objetivo_calidad(nivel) is not None:
        if not manuales_posibles(origen):
            raise ReducirError("No sé en qué precisión está este modelo, o ya es de los más chicos.")
        return "por-calidad"
    if nivel in MANUALES:
        if nivel not in manuales_posibles(origen):
            raise ReducirError(f"{nivel} no achica este modelo: elige un formato con menos bits.")
        return nivel
    if nivel == "auto":
        t = DESTINO.get(origen or "")
        if not t:
            raise ReducirError("No sé en qué precisión está este modelo, o ya es de los más chicos.")
        return t
    if nivel not in NIVELES:
        raise ReducirError("Nivel de reducción desconocido")
    if nivel not in niveles_posibles(origen):
        raise ReducirError("Este modelo ya es así de liviano o más: elige un nivel más bajo.")
    return NIVELES[nivel][0]


def candidatas(capas: int, n: int = MAX_CANDIDATAS) -> list[int]:
    """Capas que vale la pena probar quitar: de la mitad hasta la antepenúltima (las primeras y las últimas
    suelen ser imprescindibles), repartidas."""
    if capas < 12:
        return []
    zona = list(range(capas // 2, capas - 2))
    if len(zona) <= n:
        return zona
    paso = len(zona) / n
    return sorted({zona[int(i * paso)] for i in range(n)})


def leer_ppl(salida: str) -> float | None:
    m = _PPL.search(salida)
    return float(m.group(1)) if m else None


def elegir_capas(base: float, medidas: dict[int, float], tope_capa: float = PERDIDA_MAX_CAPA,
                 tope_total: float = PERDIDA_MAX_TOTAL) -> list[int]:
    """Las capas cuya eliminación (de a una) empeora menos, mientras la suma estimada no pase el tope."""
    total, elegidas = 0.0, []
    for capa, ppl in sorted(medidas.items(), key=lambda kv: kv[1]):
        perdida = ppl / base - 1
        if perdida > tope_capa or total + max(perdida, 0) > tope_total:
            continue
        elegidas.append(capa)
        total += max(perdida, 0)
    return sorted(elegidas)


def textos_de_muestra(destino: Path, raices: list[Path] | None = None) -> tuple[Path, Path]:
    """Calibración y evaluación con textos que ya están en el equipo (lecciones de la Guía en español y código
    de TISMA). Archivos DISTINTOS para calibrar y para medir: si no, la medición sería tramposa."""
    raices = raices or [Path("/usr/lib/centinela/python"), Path(__file__).resolve().parents[2]]
    archivos = []
    for r in raices:
        for patron in ("tisma_guia/lecciones.py", "tisma_code/*.py", "escudo/*.py", "centinela/*.py",
                       "tisma_bienvenida/*.py", "tisma_actualizar/*.py"):
            archivos += sorted(r.glob(patron)) + sorted(r.glob("*/" + patron))
    vistos, textos = set(), []
    for f in archivos:
        if f.name not in vistos and f.is_file():
            vistos.add(f.name)
            textos.append(f.read_text(errors="replace"))
    if len(textos) < 4:
        raise ReducirError("No encontré textos de muestra para medir el modelo.")
    destino.mkdir(parents=True, exist_ok=True)
    cal, ev = destino / "calibracion.txt", destino / "evaluacion.txt"
    cal.write_text("\n\n".join(textos[0::2])[:120_000])
    ev.write_text("\n\n".join(textos[1::2])[:40_000])
    return cal, ev


def jaula(argv: list[str], trabajo: Path) -> list[str]:
    """Sin red, sin la carpeta personal, sin capacidades: solo los modelos (lectura) y su carpeta de trabajo."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise ReducirError("bubblewrap no está instalado: por seguridad no se procesan modelos sin aislamiento")
    home = str(Path.home())
    mods = str(models.MODELS_DIR.resolve())
    return [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/run",
            "--tmpfs", home, "--ro-bind", mods, mods, "--bind", str(trabajo), str(trabajo),
            "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv",
            "--setenv", "PATH", "/usr/local/bin:/opt/llama.cpp/bin:/usr/bin:/bin", "--setenv", "HOME", home,
            "--setenv", "LANG", "C.UTF-8", "--", *argv]


_TROZO = re.compile(r"\[(\d+)\]\s*[\d.]+")                 # llama-imatrix / llama-perplexity: «[7]5.43,»
_TENSOR = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")          # llama-quantize: «[ 12/ 291] blk.0…»


def fraccion(argv: list[str], salida: str) -> float | None:
    """Cuánto lleva la herramienta (0 a 1), leyendo lo que va escribiendo."""
    if argv and argv[0] == "llama-quantize":
        m = _TENSOR.findall(salida[-2000:])
        return int(m[-1][0]) / max(1, int(m[-1][1])) if m else None
    if "--chunks" in argv:
        m = _TROZO.findall(salida[-2000:])
        return min(1.0, int(m[-1]) / max(1, int(argv[argv.index("--chunks") + 1]))) if m else None
    return None


def ejecutar(argv: list[str], trabajo: Path, avance=None) -> str:
    """Corre la herramienta aislada; si se pasa `avance`, lo llama con la fracción hecha mientras trabaja."""
    import time
    p = subprocess.Popen(jaula(argv, trabajo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    salida, ultimo, fin = "", -1.0, time.monotonic() + 6 * 3600
    try:
        while chunk := p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096):
            salida = (salida + chunk.decode(errors="replace"))[-200_000:]
            if avance:
                f = fraccion(argv, salida)
                if f is not None and f - ultimo >= 0.02:
                    ultimo = f
                    avance(f)
            if time.monotonic() > fin:
                p.kill()
                raise ReducirError(f"{argv[0]} tardó demasiado")
        rc = p.wait()
    finally:
        if p.poll() is None:
            p.kill()
    if rc != 0:
        raise ReducirError(salida[-400:].strip() or f"{argv[0]} falló")
    return salida


def hilos() -> str:
    return str(max(1, (os.cpu_count() or 2) - 1))


def reducir(modelo: str, nivel: str = "auto", quitar_capas: bool = False, run=ejecutar, avisar=lambda m: None,
            trabajo_base: Path | None = None, progreso=None) -> dict:
    """Crea la versión reducida junto al original. nivel: «auto», «normal», «liviano» o «muy_liviano» (precisión,
    siempre según la importancia medida); quitar_capas: además busca y quita capas que casi no aportan.
    Devuelve tamaños, capas quitadas y calidad medida."""
    from .motor import gguf_info, total_size
    src = Path(modelo).resolve()
    if not src.is_file() or not str(src).startswith(str(models.MODELS_DIR.resolve()) + os.sep):
        raise ReducirError("Solo se pueden reducir modelos descargados por TISMA Code")
    if models._SPLIT.search(src.name):
        raise ReducirError("Este modelo está dividido en partes: todavía no se puede reducir")
    info = gguf_info(str(src))
    origen = precision(src, info.get("tipo_archivo"))
    tipo = tipo_destino(origen, nivel)
    objetivo = objetivo_calidad(nivel)
    if objetivo is not None:
        quitar_capas = False                      # con objetivo de calidad, solo el formato (cada prueba ya cuesta)
    base_dir = trabajo_base or models.MODELS_DIR.parent / "reducir"
    trabajo = base_dir / secrets.token_hex(6)
    trabajo.mkdir(parents=True, mode=0o700)
    # Etapas: importancia, calidad del original, [cada capa candidata + la referencia], versión reducida, su calidad
    n_cand = len(candidatas(info.get("capas", 0))) if quitar_capas else 0
    total = 4 + (n_cand + 1 if quitar_capas else 0)
    if objetivo is not None:                  # importancia, original y unas log2(n)+1 pruebas
        total = 2 + max(1, len(manuales_posibles(origen))).bit_length()
    hecho = [0]
    aviso_base = avisar

    etapa = [""]

    def avisar(m: str) -> None:              # noqa: F811 — cada aviso es una etapa: avanza la barra
        aviso_base(m)
        etapa[0] = m.rstrip("…")
        if progreso:
            progreso(min(hecho[0], total - 1), total, etapa[0])
        hecho[0] += 1

    if run is ejecutar and progreso:          # dentro de cada etapa, el avance que va escribiendo la herramienta
        def run(argv, trabajo):  # noqa: F811
            base = min(hecho[0] - 1, total - 1)
            return ejecutar(argv, trabajo, avance=lambda f: progreso(round(base + f * 0.99, 3), total,
                                                                     f"{etapa[0]} ({round(f * 100)} %)"))
    try:
        cal, ev = textos_de_muestra(trabajo)
        requant = ["--allow-requantize"] if origen not in ("f32", "f16", "bf16") else []
        imatrix = trabajo / "importancia.gguf"
        avisar("Midiendo qué pesos importan (con texto de muestra)…")
        run(["llama-imatrix", "-m", str(src), "-f", str(cal), "-o", str(imatrix), "-c", "512", "--chunks", "24",
             "-t", hilos()], trabajo)

        def cuantizar(salida: Path, quitar: list[int]) -> None:
            extra = ["--prune-layers", ",".join(map(str, quitar))] if quitar else []
            run(["llama-quantize", *requant, "--imatrix", str(imatrix), *extra, str(src), str(salida), tipo, hilos()],
                trabajo)

        def ppl(m: Path) -> float:
            v = leer_ppl(run(["llama-perplexity", "-m", str(m), "-f", str(ev), "-c", "512", "--chunks", "8",
                              "-t", hilos()], trabajo))
            if v is None:
                raise ReducirError("No se pudo medir la calidad del modelo")
            return v

        avisar("Midiendo la calidad del original…")
        base = ppl(src)
        quitar: list[int] = []
        if quitar_capas:
            medidas = {}
            for capa in candidatas(info.get("capas", 0)):
                avisar(f"Probando sin la capa {capa}…")
                prueba = trabajo / "prueba.gguf"
                cuantizar(prueba, [capa])
                medidas[capa] = ppl(prueba)
                prueba.unlink(missing_ok=True)
            avisar("Midiendo la referencia sin quitar capas…")
            sin_quitar = trabajo / "sin-quitar.gguf"
            cuantizar(sin_quitar, [])
            ref = ppl(sin_quitar)                         # la referencia es la misma precisión, sin quitar capas
            sin_quitar.unlink(missing_ok=True)
            quitar = elegir_capas(ref, medidas)
        aviso_objetivo = ""
        if objetivo is not None:
            tipos = manuales_posibles(origen)
            probados: dict[str, tuple[Path, float]] = {}

            def medir(t: str) -> float:
                nonlocal tipo
                tipo = t
                avisar(f"Probando {t} (¿conserva al menos {objetivo} %?)…")
                f = trabajo / f"prueba-{t.lower()}.gguf"
                cuantizar(f, [])
                p = ppl(f)
                q = max(0.0, min(100.0, 100 * base / p)) if p else 0.0
                probados[t] = (f, p)
                return q
            elegido, medidas = buscar_por_calidad(tipos, medir, objetivo)
            if elegido is None:                       # ninguno llega: el de más calidad de los probados
                elegido = max(medidas, key=medidas.get)
                aviso_objetivo = (f"Ningún formato más chico llega al {objetivo} %: se dejó el que más conserva "
                                  f"({elegido}, {medidas[elegido]:.0f} %).")
            for t, (f, _) in probados.items():        # solo queda el elegido
                if t != elegido:
                    f.unlink(missing_ok=True)
            tipo = elegido
            nombre = f"{src.stem}-reducido-{tipo.lower()}.gguf"
            tmp = trabajo / nombre
            probados[tipo][0].rename(tmp)
            final = probados[tipo][1]
        else:
            avisar("Creando la versión reducida…")
            nombre = f"{src.stem}-reducido-{tipo.lower()}" + (f"-{len(quitar)}capas" if quitar else "") + ".gguf"
            tmp = trabajo / nombre
            cuantizar(tmp, quitar)
            avisar("Midiendo la calidad de la versión reducida…")
            final = ppl(tmp)
        if quitar and final / base - 1 > PERDIDA_MAX_TOTAL * 1.5:   # juntas empeoraron de más: sin quitar capas
            avisar("Quitar capas empeoraba demasiado: se conservan todas.")
            quitar = []
            nombre = f"{src.stem}-reducido-{tipo.lower()}.gguf"
            tmp.unlink(missing_ok=True)
            tmp = trabajo / nombre
            cuantizar(tmp, [])
            final = ppl(tmp)
        destino = src.parent / nombre
        shutil.move(str(tmp), destino)
        if progreso:
            progreso(total, total, "Listo")
        calidad = max(0.0, min(100.0, 100 * base / final)) if final else 0.0
        return {"archivo": str(destino), "antes": total_size(src), "despues": destino.stat().st_size,
                "tipo": tipo, "capas_quitadas": quitar, "capas": info.get("capas", 0),
                "ppl_original": round(base, 3), "ppl_reducido": round(final, 3), "calidad": round(calidad, 1),
                **({"objetivo": objetivo, "aviso": aviso_objetivo} if objetivo is not None else {})}
    finally:
        shutil.rmtree(trabajo, ignore_errors=True)
