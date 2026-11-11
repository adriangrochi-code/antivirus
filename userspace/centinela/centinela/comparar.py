"""centinela comparar: activa un ajuste, mide, y solo lo deja si gana.

Cada «ajuste» sabe guardar el estado actual, aplicarse y volver exactamente al estado guardado. Se mide en
rondas alternas A-B-B-A (A = sin el ajuste, B = con él) para que el calentamiento del equipo o una tarea de
fondo no favorezcan siempre al mismo lado, y se decide con una prueba estadística (U de Mann-Whitney exacta,
sin suponer que el ruido es normal): un ajuste se queda solo si alguna medida mejora de forma clara y
ninguna empeora. Así cada «optimización» está respaldada por números de ESTE equipo.

Los kernels no se pueden cambiar en caliente: «registrar» guarda medidas en cada arranque (con el kernel que
haya arrancado) y «kernels» compara cuando hay suficientes arranques de cada uno.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

from . import bench

ESTADO = os.environ.get("CENTINELA_COMPARAR_DIR", "/var/lib/centinela/comparar")
ALFA = 0.05                  # nivel de la prueba (una cola)
GANANCIA_MIN = 2.0           # % mínimo para considerar que algo mejora (por debajo, no vale el cambio)
PERDIDA_MAX = 3.0            # % de empeoramiento claro que descarta un ajuste aunque mejore otra cosa


# ---------------------------------------------------------------- estadística
def u_stat(a: list[float], b: list[float]) -> float:
    """U de Mann-Whitney: cuántos pares (x de b, y de a) tienen x > y (los empates cuentan medio)."""
    return sum(1.0 if x > y else 0.5 if x == y else 0.0 for x in b for y in a)


def p_mayor(a: list[float], b: list[float], max_perm: int = 300_000) -> float:
    """P (una cola) de que b sea mayor que a por azar: prueba exacta de permutaciones sobre U (admite empates).
    Con muchas muestras, aproximación normal."""
    n, m = len(a), len(b)
    if not n or not m:
        return 1.0
    obs = u_stat(a, b)
    todos = a + b
    total = 1
    for k in range(m):
        total = total * (n + m - k) // (k + 1)
    if total <= max_perm:
        ge = 0
        for idx in itertools.combinations(range(n + m), m):
            s = set(idx)
            bb = [todos[i] for i in idx]
            aa = [todos[i] for i in range(n + m) if i not in s]
            if u_stat(aa, bb) >= obs - 1e-9:
                ge += 1
        return ge / total
    mu, sd = n * m / 2, (n * m * (n + m + 1) / 12) ** 0.5
    z = (obs - 0.5 - mu) / sd
    return 0.5 * (1 - math.erf(z / 2 ** 0.5))


# ---------------------------------------------------------------- medidas
@dataclass
class Metrica:
    nombre: str
    mayor_mejor: bool
    unidad: str


METRICAS = {
    "memoria.lectura_gbs": Metrica("Ancho de banda de memoria (lectura)", True, "GB/s"),
    "memoria.latencia_ram_ns": Metrica("Latencia de la RAM", False, "ns"),
    "planificador.pingpong_us": Metrica("Despertar un proceso (ida y vuelta)", False, "µs"),
    "planificador.timer_p99_us": Metrica("Retraso del temporizador (p99)", False, "µs"),
    "ia.generacion_ts": Metrica("IA: tokens/s generando", True, "tok/s"),
    "ia.prompt_ts": Metrica("IA: tokens/s leyendo", True, "tok/s"),
    "arranque.total_s": Metrica("Tiempo de arranque", False, "s"),
}


def _membench(args: list[str]) -> dict:
    exe = bench._membench_bin()
    if not exe:
        raise RuntimeError("centinela-membench no está instalado")
    return json.loads(subprocess.run([exe, *args], capture_output=True, text=True, timeout=300, check=True).stdout)


def prueba_memoria(_ctx: dict) -> dict[str, float]:
    d = _membench(["--quick"])
    return {"memoria.lectura_gbs": d["bandwidth_gbs"]["read"],
            "memoria.latencia_ram_ns": d["latency_ns"]["262144"]}


def prueba_planificador(_ctx: dict) -> dict[str, float]:
    d = _membench(["--sched"])
    if "pingpong_us" not in d:
        raise RuntimeError("centinela-membench es de una versión sin «--sched»: reinstala Centinela")
    return {"planificador.pingpong_us": d["pingpong_us"], "planificador.timer_p99_us": d["timer_p99_us"]}


def _jaula_llama(argv: list[str], modelo: str) -> list[str]:
    """llama-bench en bubblewrap: lee un GGUF (entrada no confiable) y no necesita red ni archivos personales."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bubblewrap no está instalado: la prueba de IA no se ejecuta sin aislamiento")
    carpeta = os.path.dirname(os.path.realpath(modelo))
    gpu: list[str] = []
    for d in ("/dev/dri", "/dev/kfd"):
        gpu += ["--dev-bind-try", d, d]
    return [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", *gpu, "--proc", "/proc", "--tmpfs", "/tmp",
            "--tmpfs", "/home", "--tmpfs", "/root", "--tmpfs", "/run", "--ro-bind", carpeta, carpeta,
            "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv",
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--setenv", "HOME", "/tmp", "--", *argv]


def prueba_ia(ctx: dict) -> dict[str, float]:
    """tokens/s de llama.cpp con el modelo indicado (la misma configuración en A y en B)."""
    modelo = ctx.get("modelo")
    if not modelo:
        return {}
    exe = shutil.which("llama-bench") or "/opt/llama.cpp/bin/llama-bench"
    argv = [exe, "-m", os.path.realpath(modelo), "-p", "256", "-n", "32", "-r", "2", "-o", "json",
            "-ngl", str(ctx.get("ngl", 0))]
    r = subprocess.run(_jaula_llama(argv, modelo), capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"llama-bench falló: {(r.stderr or r.stdout)[-300:]}")
    out = {}
    for e in json.loads(r.stdout):
        if e.get("n_gen"):
            out["ia.generacion_ts"] = float(e["avg_ts"])
        elif e.get("n_prompt"):
            out["ia.prompt_ts"] = float(e["avg_ts"])
    return out


PRUEBAS: dict[str, Callable[[dict], dict[str, float]]] = {
    "memoria": prueba_memoria, "planificador": prueba_planificador, "ia": prueba_ia}


# ---------------------------------------------------------------- ajustes
def _leer(path: str) -> str:
    with open(path) as fh:
        return fh.read().strip()


def _escribir(path: str, valor: str) -> None:
    with open(path, "w") as fh:
        fh.write(valor)


def _sel(texto: str) -> str:
    """De «always [madvise] never» a «madvise»."""
    for w in texto.split():
        if w.startswith("[") and w.endswith("]"):
            return w[1:-1]
    return texto


@dataclass
class Ajuste:
    id: str
    descripcion: str
    guardar: Callable[[], dict]
    aplicar: Callable[[], None]
    restaurar: Callable[[dict], None]
    disponible: Callable[[], str | None] = lambda: None          # None = disponible; si no, el motivo
    pruebas: list[str] = field(default_factory=lambda: ["memoria", "planificador", "ia"])


THP = "/sys/kernel/mm/transparent_hugepage"


def _thp_guardar() -> dict:
    return {"enabled": _sel(_leer(f"{THP}/enabled")), "defrag": _sel(_leer(f"{THP}/defrag"))}


def _thp_restaurar(s: dict) -> None:
    _escribir(f"{THP}/enabled", s["enabled"])
    _escribir(f"{THP}/defrag", s["defrag"])


def _thp_aplicar() -> None:
    _escribir(f"{THP}/enabled", "always")
    _escribir(f"{THP}/defrag", "defer+madvise")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60)


def _scx_disponible() -> str | None:
    if not os.path.exists("/sys/kernel/sched_ext/state"):
        return "este kernel no tiene sched_ext"
    if _systemctl("is-active", "tisma-scx").stdout.strip() != "active":
        return "tisma-scx no está en marcha: no hay nada que quitar"
    return None


AJUSTES: dict[str, Ajuste] = {
    "thp-siempre": Ajuste(
        "thp-siempre", "Páginas grandes (THP) para toda la memoria de los programas, no solo la que las pide. "
        "Menos fallos de la caché de traducción (TLB): puede acelerar la IA con el modelo cargado en RAM.",
        _thp_guardar, _thp_aplicar, _thp_restaurar,
        lambda: None if os.access(f"{THP}/enabled", os.W_OK) else "THP no disponible o sin permisos"),
    "sin-scx": Ajuste(
        "sin-scx", "Planificador del kernel (EEVDF) en vez del de sched_ext (scx_lavd/bpfland): comprueba si "
        "el planificador extra de verdad ayuda en este equipo.",
        lambda: {"activo": True}, lambda: _systemctl("stop", "tisma-scx"),
        lambda s: _systemctl("start", "tisma-scx") if s.get("activo") else None,
        _scx_disponible, ["planificador", "ia"]),
}


# ---------------------------------------------------------------- comparación
def _resumen(nombre: str, a: list[float], b: list[float]) -> dict:
    meta = METRICAS.get(nombre, Metrica(nombre, True, ""))
    ma, mb = statistics.median(a), statistics.median(b)
    cambio = 0.0 if ma == 0 else (mb - ma) / abs(ma) * 100
    mejora = cambio if meta.mayor_mejor else -cambio          # % positivo = mejor con el ajuste
    p_mejor = p_mayor(a, b) if meta.mayor_mejor else p_mayor(b, a)
    p_peor = p_mayor(b, a) if meta.mayor_mejor else p_mayor(a, b)
    return {"metrica": nombre, "nombre": meta.nombre, "unidad": meta.unidad, "sin": round(ma, 3),
            "con": round(mb, 3), "mejora_pct": round(mejora, 2), "p_mejor": round(p_mejor, 4),
            "p_peor": round(p_peor, 4), "n": [len(a), len(b)]}


def veredicto(filas: list[dict]) -> str:
    """«mejor» si algo mejora claramente y nada empeora claramente; «peor» si algo empeora; si no, «igual»."""
    peor = any(f["p_peor"] < ALFA and f["mejora_pct"] <= -PERDIDA_MAX for f in filas)
    mejor = any(f["p_mejor"] < ALFA and f["mejora_pct"] >= GANANCIA_MIN for f in filas)
    if peor:
        return "peor"
    return "mejor" if mejor else "igual"


def comparar_muestras(a: dict[str, list[float]], b: dict[str, list[float]]) -> tuple[list[dict], str]:
    filas = [_resumen(k, a[k], b[k]) for k in sorted(set(a) & set(b)) if a[k] and b[k]]
    return filas, veredicto(filas)


def condiciones(forzar: bool = False) -> str | None:
    """Medir con el equipo ocupado o a batería da números que no sirven: se avisa (o se mide igual con --forzar)."""
    if forzar:
        return None
    try:
        carga = os.getloadavg()[0]
        if carga > max(1.0, (os.cpu_count() or 1) * 0.25):
            return f"el equipo está ocupado (carga {carga:.1f}): cierra programas o usa --forzar"
    except OSError:
        pass
    for ps in sorted(os.listdir("/sys/class/power_supply")) if os.path.isdir("/sys/class/power_supply") else []:
        base = f"/sys/class/power_supply/{ps}"
        try:
            if _leer(f"{base}/type") == "Mains" and _leer(f"{base}/online") == "0":
                return "el equipo está a batería (la CPU baja la frecuencia): conecta el cargador o usa --forzar"
        except OSError:
            continue
    return None


def ejecutar(ajuste: Ajuste, rondas: int = 4, ctx: dict | None = None, dejar: bool = False,
             progreso: Callable[[str], None] = print, pausa: float = 2.0,
             pruebas: dict[str, Callable] | None = None) -> dict:
    """Rondas A-B-B-A. Siempre vuelve al estado original salvo que gane y se pida dejarlo (--dejar)."""
    ctx = ctx or {}
    pruebas = pruebas if pruebas is not None else PRUEBAS
    usar = [p for p in ajuste.pruebas if p in pruebas and (p != "ia" or ctx.get("modelo"))]
    if not usar:
        raise RuntimeError("no hay pruebas que aplicar a este ajuste (¿falta --modelo para la de IA?)")
    original = ajuste.guardar()
    muestras = {"A": {}, "B": {}}
    queda = False
    try:
        for i in range(rondas):
            for lado in ("A", "B") if i % 2 == 0 else ("B", "A"):
                if lado == "B":
                    ajuste.aplicar()
                else:
                    ajuste.restaurar(original)
                time.sleep(pausa)
                for nombre in usar:
                    for k, v in pruebas[nombre](ctx).items():
                        muestras[lado].setdefault(k, []).append(v)
                progreso(f"Ronda {i + 1}/{rondas}: medido {'con' if lado == 'B' else 'sin'} «{ajuste.id}»")
        filas, ver = comparar_muestras(muestras["A"], muestras["B"])
        queda = dejar and ver == "mejor"
        if queda:
            ajuste.aplicar()
    finally:
        if not queda:
            ajuste.restaurar(original)
    res = {"ajuste": ajuste.id, "fecha": time.strftime("%Y-%m-%dT%H:%M:%S"), "rondas": rondas,
           "veredicto": ver, "aplicado": queda, "filas": filas, "muestras": muestras}
    _guardar_resultado(res)
    return res


def _guardar_resultado(res: dict) -> None:
    os.makedirs(ESTADO, exist_ok=True)
    with open(os.path.join(ESTADO, f"{res['ajuste']}.json"), "w") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    dec_path = os.path.join(ESTADO, "decisiones.json")
    dec = _leer_json(dec_path)
    dec[res["ajuste"]] = {"aplicado": res["aplicado"], "veredicto": res["veredicto"], "fecha": res["fecha"]}
    fd, tmp = tempfile.mkstemp(dir=ESTADO)
    with os.fdopen(fd, "w") as fh:
        json.dump(dec, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, dec_path)


def _leer_json(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def reaplicar(progreso: Callable[[str], None] = print) -> list[str]:
    """En cada arranque: vuelve a aplicar los ajustes que ganaron (los de sysfs se pierden al reiniciar)."""
    hechos = []
    for aid, d in _leer_json(os.path.join(ESTADO, "decisiones.json")).items():
        aj = AJUSTES.get(aid)
        if not aj or not d.get("aplicado") or aj.disponible():
            continue
        try:
            aj.aplicar()
            hechos.append(aid)
        except OSError as e:
            progreso(f"No se pudo reaplicar «{aid}»: {e}")
    return hechos


# ---------------------------------------------------------------- kernels (entre arranques)
def tiempo_arranque() -> float | None:
    r = subprocess.run(["systemd-analyze", "time"], capture_output=True, text=True, timeout=60)
    m = re.search(r"=\s*(?:(\d+)min\s*)?([\d.]+)s", r.stdout)
    return (int(m.group(1) or 0) * 60 + float(m.group(2))) if r.returncode == 0 and m else None


def registrar(kernel: str | None = None, pruebas: dict[str, Callable] | None = None,
              arranque: Callable[[], float | None] = tiempo_arranque) -> dict:
    """Medidas de este arranque, etiquetadas con el kernel. Solo con varios kernels instalados (si no, no hay
    nada que comparar) y con el equipo libre y enchufado."""
    kernel = kernel or os.uname().release
    pruebas = pruebas if pruebas is not None else {k: v for k, v in PRUEBAS.items() if k != "ia"}
    fila: dict = {"kernel": kernel, "fecha": time.strftime("%Y-%m-%dT%H:%M:%S")}
    t = arranque()
    if t is not None:
        fila["arranque.total_s"] = t
    for p in pruebas.values():
        fila.update(p({}))
    os.makedirs(ESTADO, exist_ok=True)
    with open(os.path.join(ESTADO, "arranques.jsonl"), "a") as fh:
        fh.write(json.dumps(fila) + "\n")
    return fila


def kernels(minimo: int = 3) -> dict:
    """Compara el kernel con más arranques (referencia) contra cada uno de los demás."""
    por: dict[str, dict[str, list[float]]] = {}
    try:
        with open(os.path.join(ESTADO, "arranques.jsonl")) as fh:
            for line in fh:
                try:
                    f = json.loads(line)
                except ValueError:
                    continue
                d = por.setdefault(f.get("kernel", "?"), {})
                for k, v in f.items():
                    if k in METRICAS and isinstance(v, (int, float)):
                        d.setdefault(k, []).append(float(v))
    except OSError:
        pass
    cuenta = {k: max((len(v) for v in d.values()), default=0) for k, d in por.items()}
    listos = [k for k, n in cuenta.items() if n >= minimo]
    out = {"arranques": cuenta, "comparaciones": []}
    if len(listos) < 2:
        out["falta"] = (f"Hacen falta al menos {minimo} arranques con cada uno de dos kernels "
                        f"(hay: {', '.join(f'{k}: {n}' for k, n in cuenta.items()) or 'ninguno'})")
        return out
    ref = max(listos, key=lambda k: cuenta[k])
    for k in listos:
        if k != ref:
            filas, ver = comparar_muestras(por[ref], por[k])
            out["comparaciones"].append({"referencia": ref, "kernel": k, "veredicto": ver, "filas": filas})
    return out


# ---------------------------------------------------------------- salida
def tabla(filas: list[dict]) -> str:
    out = []
    for f in filas:
        claro = "claro" if min(f["p_mejor"], f["p_peor"]) < ALFA else "ruido"
        out.append(f"  {f['nombre']:<38} {f['sin']:>9g} → {f['con']:>9g} {f['unidad']:<5} "
                   f"{f['mejora_pct']:+6.1f} %  ({claro}, p={min(f['p_mejor'], f['p_peor']):.3f})")
    return "\n".join(out)


TEXTO_VEREDICTO = {"mejor": "MEJORA: alguna medida gana claramente y ninguna empeora",
                   "peor": "EMPEORA: alguna medida pierde claramente",
                   "igual": "SIN DIFERENCIA CLARA: no vale la pena el cambio"}
