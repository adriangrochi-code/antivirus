"""Banco de pruebas de rendimiento por vatio (documento de traspaso, sección 8). Hacer ANTES de integrar nada.

Cada tarea se repite N veces con la misma temperatura de partida (se espera a que el equipo se enfríe) y se
registra siempre: tiempo, vatios medios, julios, temperatura inicial y máxima, y presión (PSI) de CPU y memoria.
Salida: un CSV por corrida (una fila por métrica y repetición) y `centinela banco tabla` para compararlas.

Las configuraciones a comparar se nombran con --etiqueta: «debian» (tras `centinela fabrica` y reiniciar: línea
base), «debian-tlp» (con TLP y thermald instalados: línea base afinada), «tisma-eco», «tisma-normal»,
«tisma-maximo»… La energía sale de la batería (`power_now`, ruidosa: el equipo tiene que estar A BATERÍA); con
un medidor externo es mejor y se puede anotar aparte.

Tareas: inventario, reposo, ancho_banda, descompresion, compresion_modelo, almacenamiento, arranque,
escritorio, compilacion, inferencia. (Navegación, multimedia y juegos necesitan un guion con ventanas: van
aparte.)
"""
from __future__ import annotations

import csv
import glob
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import sysfs

COLUMNAS = ["fecha", "equipo", "kernel", "etiqueta", "modo", "tarea", "variante", "repeticion", "metrica",
            "valor", "unidad", "segundos", "vatios_medios", "julios", "temp_inicio_c", "temp_max_c",
            "psi_cpu", "psi_mem", "a_bateria"]


def sh(argv: list[str], timeout: int = 600) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------- energía, temperatura y presión
def bateria() -> tuple[float | None, bool]:
    """(vatios que entrega la batería ahora, ¿está descargándose?)."""
    for b in sysfs.ls("/sys/class/power_supply/BAT*"):
        st = sysfs.read(f"{b}/status") or ""
        p = sysfs.read_int(f"{b}/power_now")
        if p is None:
            i, v = sysfs.read_int(f"{b}/current_now"), sysfs.read_int(f"{b}/voltage_now")
            p = i * v // 1_000_000 if i is not None and v is not None else None
        if p is not None:
            return p / 1e6, st == "Discharging"
    return None, False


def temperatura() -> float | None:
    from .calor import temperatura_cpu
    return temperatura_cpu()


def psi(recurso: str) -> float | None:
    m = re.search(r"some avg10=([\d.]+)", sysfs.read(f"/proc/pressure/{recurso}", "") or "")
    return float(m.group(1)) if m else None


class Medidor:
    """Muestrea batería y temperatura cada segundo mientras corre la tarea."""

    def __init__(self, paso: float = 1.0):
        self.paso, self.w, self.t = paso, [], []
        self._fin = threading.Event()
        self.bateria = False

    def _bucle(self):
        while not self._fin.wait(self.paso):
            w, desc = bateria()
            if w is not None and desc:
                self.w.append(w)
            t = temperatura()
            if t is not None:
                self.t.append(t)

    def __enter__(self):
        self.t0 = time.monotonic()
        self.temp_inicio = temperatura()
        _, self.bateria = bateria()
        self._h = threading.Thread(target=self._bucle, daemon=True)
        self._h.start()
        return self

    def __exit__(self, *a):
        self._fin.set()
        self._h.join(timeout=5)
        self.segundos = time.monotonic() - self.t0
        self.vatios = statistics.fmean(self.w) if self.w else None
        self.julios = self.vatios * self.segundos if self.vatios is not None else None
        self.temp_max = max(self.t) if self.t else self.temp_inicio
        return False


def enfriar(objetivo: float | None, max_s: float = 180, espera=time.sleep) -> None:
    """Misma temperatura de partida en cada repetición (como mucho 3 min de espera)."""
    if objetivo is None:
        return
    fin = time.monotonic() + max_s
    while time.monotonic() < fin:
        t = temperatura()
        if t is None or t <= objetivo + 2:
            return
        espera(5)


# ---------------------------------------------------------------- tareas (devuelven {métrica: (valor, unidad)})
def _membench() -> str | None:
    from .bench import _membench_bin
    return _membench_bin()


def t_reposo(minutos: float = 10, **_) -> dict:
    """Escritorio inactivo, pantalla encendida: solo se mide (los W los toma el Medidor)."""
    time.sleep(minutos * 60)
    return {"duracion": (minutos * 60, "s")}


def t_ancho_banda(hilos: int = 1, **_) -> dict:
    exe = _membench()
    if not exe:
        return {}
    d = json.loads(sh([exe, "--bw", str(hilos)]) or "{}")
    return {k: (d[k], "GB/s") for k in ("read", "write", "copy") if k in d}


def _muestra(mb: int = 64) -> str:
    from .bench import _sample_file
    return _sample_file(mb)


def t_descompresion(hilos: int = 1, algoritmo: str = "lz4", **_) -> dict:
    """Velocidad de descompresión con N procesos a la vez (zstd/lz4 en modo banco, sumada)."""
    exe = shutil.which("zstd" if algoritmo.startswith("zstd") else "lz4")
    if not exe:
        return {}
    nivel = "-1" if algoritmo in ("lz4", "zstd") else "-" + algoritmo.split("-")[-1]
    f = _muestra(64)
    try:
        cmd = [exe, "-b" + nivel.lstrip("-"), "-e" + nivel.lstrip("-")] + (["-T1"] if exe.endswith("zstd") else [])
        procs = [subprocess.Popen(cmd + [f], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                 for _ in range(hilos)]
        salidas = [p.communicate(timeout=600)[0] for p in procs]
    finally:
        os.unlink(f)
    dec, ratio = [], []
    for s in salidas:
        m = re.findall(r"\(x?([\d.]+)\),\s*([\d.]+)\s*MB/s[ ,]+([\d.]+)\s*MB/s", s)
        if m:
            ratio.append(float(m[-1][0]))
            dec.append(float(m[-1][2]))
    if not dec:
        return {}
    return {"descompresion": (round(sum(dec) / 1000, 3), "GB/s"), "razon": (statistics.fmean(ratio), "x")}


def t_compresion_modelo(modelo: str = "", muestras: int = 3, mb: int = 64, **_) -> dict:
    """Sección 6.2, paso 1: razón de compresión EXTRA de un GGUF ya cuantizado (si < 1,1 el experimento para)."""
    if not modelo or not os.path.isfile(modelo):
        return {}
    tam = os.path.getsize(modelo)
    trozo = mb << 20
    out = {}
    for nombre, cmd in (("zstd19", ["zstd", "-19", "-T0", "-c"]), ("xz9", ["xz", "-9", "-T0", "-c"])):
        if not shutil.which(cmd[0]):
            continue
        total_in = total_out = 0
        with open(modelo, "rb") as fh:
            for i in range(muestras):
                fh.seek(max(0, (tam - trozo) * i // max(1, muestras - 1)))
                datos = fh.read(trozo)
                r = subprocess.run(cmd, input=datos, capture_output=True, timeout=1800)
                total_in, total_out = total_in + len(datos), total_out + len(r.stdout)
        if total_out:
            out[f"razon_{nombre}"] = (round(total_in / total_out, 4), "x")
    return out


def t_almacenamiento(**_) -> dict:
    """fio 4K aleatorio (profundidad 1 y 32) sobre un archivo temporal; sin fio, lectura directa simple."""
    if not shutil.which("fio"):
        return {}
    out = {}
    with tempfile.TemporaryDirectory(dir="/var/tmp") as d:
        for qd in (1, 32):
            j = sh(["fio", "--name=b", f"--directory={d}", "--size=512M", "--rw=randread", "--bs=4k",
                    f"--iodepth={qd}", "--ioengine=libaio", "--direct=1", "--runtime=20", "--time_based",
                    "--output-format=json"])
            try:
                r = json.loads(j)["jobs"][0]["read"]
            except (ValueError, KeyError, IndexError):
                continue
            out[f"iops_qd{qd}"] = (round(r["iops"]), "IOPS")
            p99 = r.get("clat_ns", {}).get("percentile", {}).get("99.000000")
            if p99:
                out[f"p99_qd{qd}"] = (round(p99 / 1000, 1), "us")
    return out


def t_arranque(**_) -> dict:
    """systemd-analyze del ÚLTIMO arranque (para comparar hay que reiniciar entre repeticiones)."""
    s = sh(["systemd-analyze", "time"])
    total = re.search(r"=\s*(?:(\d+)min\s*)?([\d.]+)s", s)
    out = {}
    if total:
        out["hasta_sistema"] = (int(total.group(1) or 0) * 60 + float(total.group(2)), "s")
    try:
        from .boot import tiempo_sesion
        t = tiempo_sesion()
        if t:
            out["hasta_escritorio"] = (round(t, 1), "s")
    except Exception:  # noqa: BLE001
        pass
    return out


def t_escritorio(**_) -> dict:
    """Latencia del planificador con la CPU llena (compilación simulada): p99 del temporizador y ping-pong."""
    exe = _membench()
    estres = shutil.which("centinela-estres") or "/usr/lib/centinela/centinela-estres"
    if not exe:
        return {}
    carga = subprocess.Popen([estres, "cpu", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) \
        if os.access(estres, os.X_OK) else None
    try:
        time.sleep(2)
        d = json.loads(sh([exe, "--sched"]) or "{}")
    finally:
        if carga:
            carga.terminate()
            carga.wait(10)
    return {"timer_p99": (d.get("timer_p99_us"), "us"), "pingpong": (d.get("pingpong_us"), "us")} if d else {}


def t_compilacion(**_) -> dict:
    """Compilación fija y repetible: las herramientas de Centinela 20 veces con -O2 (sin red ni proyecto externo)."""
    cc = shutil.which("cc") or shutil.which("gcc")
    fuentes = [p for p in (Path(__file__).resolve().parents[1] / "tools").glob("*.c")] or \
        [Path(p) for p in glob.glob("/usr/share/centinela/tools/*.c")]
    if not cc or not fuentes:
        return {}
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as d:
        for i in range(20):
            for f in fuentes:
                subprocess.run([cc, "-O2", "-pthread", "-o", f"{d}/x{i}", str(f)], capture_output=True)
    return {"tiempo": (round(time.monotonic() - t0, 2), "s")}


def t_inferencia(modelo: str = "", hilos: int = 0, **_) -> dict:
    """llama-bench (en la jaula del motor): tokens/s generando y leyendo el pedido."""
    exe = shutil.which("llama-bench")
    if not exe or not modelo:
        return {}
    from .comparar import _jaula_llama
    argv = [exe, "-m", modelo, "-p", "128", "-n", "64", "-r", "1", "-o", "json"] + \
        (["-t", str(hilos)] if hilos else [])
    try:
        filas = json.loads(sh(_jaula_llama(argv, modelo), timeout=1800) or "[]")
    except ValueError:
        return {}
    out = {}
    for f in filas:
        clave = "generar" if f.get("n_gen") else "leer"
        out[clave] = (round(f.get("avg_ts", 0), 2), "tok/s")
    return out


TAREAS = {"reposo": t_reposo, "ancho_banda": t_ancho_banda, "descompresion": t_descompresion,
          "compresion_modelo": t_compresion_modelo, "almacenamiento": t_almacenamiento, "arranque": t_arranque,
          "escritorio": t_escritorio, "compilacion": t_compilacion, "inferencia": t_inferencia}
# Variantes por defecto (sección 8: ancho de banda y descompresión con 1, 2 y 4 hilos)
VARIANTES = {"ancho_banda": [{"hilos": 1}, {"hilos": 2}, {"hilos": 4}],
             "descompresion": [{"hilos": h, "algoritmo": a} for a in ("lz4", "zstd") for h in (1, 4)]}


# ---------------------------------------------------------------- inventario (sección 2)
def inventario(run=sh) -> dict:
    """Lo que hay que confirmar del equipo antes de decidir nada (necesita root para dmidecode y dmesg)."""
    def leer_glob(pat):
        return {Path(p).name: (sysfs.read(p) or "") for p in sorted(glob.glob(pat))}
    hermanos = sorted({sysfs.read(p) for p in glob.glob(
        "/sys/devices/system/cpu/cpu[0-9]*/topology/thread_siblings_list")} - {None})
    return {
        "cpu": run(["lscpu"]),
        "memoria_dmi": run(["dmidecode", "-t", "memory"]),
        "equipo_dmi": run(["dmidecode", "-t", "system"]),
        "modulos_cmt": hermanos,          # en Excavator (CMT) los dos núcleos de cada módulo salen como hermanos
        "edac": sorted(os.listdir("/sys/devices/system/edac/mc")) if os.path.isdir("/sys/devices/system/edac/mc")
        else [],
        "vulnerabilidades": leer_glob("/sys/devices/system/cpu/vulnerabilities/*"),
        "iommu": [l for l in run(["dmesg"]).splitlines() if re.search(r"iommu|amd-vi", l, re.I)][:20],
        "tpm": sorted(glob.glob("/dev/tpm*")),
        "grafica": run(["lspci", "-k"]),
        "cpufreq": {"driver": sysfs.read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver"),
                    "governors": sysfs.read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors"),
                    "boost": sysfs.read("/sys/devices/system/cpu/cpufreq/boost"),
                    "epp": sysfs.read("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference")},
        "rapl": sorted(glob.glob("/sys/class/powercap/*rapl*")),
        "bateria": {Path(b).name: {k: sysfs.read(f"{b}/{k}") for k in
                                   ("status", "energy_now", "power_now", "current_now", "voltage_now")}
                    for b in sysfs.ls("/sys/class/power_supply/BAT*")},
        "kernel": platform.release(),
        "cmdline": sysfs.read("/proc/cmdline"),
    }


# ---------------------------------------------------------------- configuraciones a comparar
LINEA_AFINADA = ["tlp", "thermald"]


def _instalado(paquete: str, run=sh) -> bool:
    return "install ok installed" in run(["dpkg-query", "-W", "-f=${Status}", paquete])


def preparar(config: str, run=sh, estado: Path | None = None) -> list[str]:
    """Deja el equipo listo para medir una configuración (sección 8):
    * «debian-tlp»: línea base afinada = Debian sin TISMA + TLP y thermald (se instalan y activan solos).
    * «debian» o «tisma»: quita TLP y thermald, pero SOLO si los instaló el banco (lo anota en su estado).
    Devuelve los pasos hechos o avisos, en simple."""
    from .apply import STATE_DIR, en_fabrica
    marca = estado or Path(STATE_DIR) / "banco-instalados.json"
    try:
        nuestros = json.loads(marca.read_text())
    except (OSError, ValueError):
        nuestros = []
    pasos = []
    if config == "debian-tlp":
        if not en_fabrica():
            return ["Primero «sudo centinela fabrica» y reinicia: TISMA y TLP no deben ajustar el equipo a la vez."]
        faltan = [p for p in LINEA_AFINADA if not _instalado(p, run)]
        if faltan:
            run(["apt-get", "install", "-y", "--no-install-recommends", *faltan], timeout=1800)
            nuevos = [p for p in faltan if _instalado(p, run)]
            nuestros = sorted(set(nuestros) | set(nuevos))
            marca.parent.mkdir(parents=True, exist_ok=True)
            marca.write_text(json.dumps(nuestros))
            pasos.append(f"Instalados: {', '.join(nuevos) or 'ninguno (¿sin conexión?)'}")
        for svc in LINEA_AFINADA:
            if _instalado(svc, run):
                run(["systemctl", "enable", "--now", f"{svc}.service"])
        if not run(["systemctl", "is-active", "thermald"]).strip().startswith("active"):
            pasos.append("thermald no corre en este procesador (solo funciona con Intel): la línea base queda con TLP.")
        pasos.append("Listo: mide con --etiqueta debian-tlp.")
    elif config in ("debian", "tisma"):
        quitar = [p for p in nuestros if _instalado(p, run)]
        if quitar:
            for svc in quitar:
                run(["systemctl", "disable", "--now", f"{svc}.service"])
            run(["apt-get", "purge", "-y", *quitar], timeout=1800)
            pasos.append(f"Quitados (los había instalado el banco): {', '.join(quitar)}")
        marca.unlink(missing_ok=True)
        if config == "tisma":
            pasos.append("Para medir TISMA: «sudo centinela apply --reactivar», reinicia y elige el modo.")
        else:
            pasos.append("Para medir Debian sin ajustes: «sudo centinela fabrica» y reinicia (si no lo hiciste).")
    else:
        pasos.append("Configuraciones: debian, debian-tlp, tisma")
    return pasos


# ---------------------------------------------------------------- corrida y CSV
def equipo() -> str:
    return " ".join(filter(None, (sysfs.read("/sys/class/dmi/id/sys_vendor"),
                                  sysfs.read("/sys/class/dmi/id/product_version") or
                                  sysfs.read("/sys/class/dmi/id/product_name")))) or platform.node()


def modo_actual() -> str:
    from .apply import load_state
    return (load_state("prefs.json") or {}).get("perfil", "")


def correr(tareas: list[str], etiqueta: str, repeticiones: int = 3, salida: str = "", opciones: dict | None = None,
           progreso=print) -> str:
    opciones = opciones or {}
    salida = salida or f"banco-{etiqueta}-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    base = temperatura()
    nuevo = not os.path.exists(salida)
    with open(salida, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNAS)
        if nuevo:
            w.writeheader()
        for tarea in tareas:
            for variante in VARIANTES.get(tarea, [{}]):
                nombre_var = ",".join(f"{k}={v}" for k, v in variante.items())
                for rep in range(1, repeticiones + 1):
                    enfriar(base)
                    progreso(f"· {tarea} {nombre_var} ({rep}/{repeticiones})")
                    with Medidor() as m:
                        res = TAREAS[tarea](**{**opciones, **variante})
                    for metrica, (valor, unidad) in res.items():
                        if valor is None:
                            continue
                        w.writerow({"fecha": time.strftime("%Y-%m-%dT%H:%M:%S"), "equipo": equipo(),
                                    "kernel": platform.release(), "etiqueta": etiqueta, "modo": modo_actual(),
                                    "tarea": tarea, "variante": nombre_var, "repeticion": rep, "metrica": metrica,
                                    "valor": valor, "unidad": unidad, "segundos": round(m.segundos, 2),
                                    "vatios_medios": round(m.vatios, 3) if m.vatios is not None else "",
                                    "julios": round(m.julios, 1) if m.julios is not None else "",
                                    "temp_inicio_c": m.temp_inicio or "", "temp_max_c": m.temp_max or "",
                                    "psi_cpu": psi("cpu"), "psi_mem": psi("memory"), "a_bateria": m.bateria})
                    fh.flush()
    return salida


def tabla(archivos: list[str]) -> str:
    """Media ± desviación por configuración, y diferencia con la primera etiqueta (la línea base)."""
    grupos: dict[tuple, list[float]] = {}
    etiquetas: list[str] = []
    vistos: set[tuple] = set()           # julios: uno por repetición (la fila se repite por cada métrica)
    for a in archivos:
        with open(a, encoding="utf-8") as fh:
            for f in csv.DictReader(fh):
                try:
                    v = float(f["valor"])
                except (ValueError, KeyError):
                    continue
                if f["etiqueta"] not in etiquetas:
                    etiquetas.append(f["etiqueta"])
                grupos.setdefault((f["tarea"], f["variante"], f["metrica"], f["unidad"], f["etiqueta"]), []).append(v)
                j = f.get("julios")
                rep_id = (a, f["etiqueta"], f["tarea"], f["variante"], f["repeticion"], f["fecha"][:16])
                if j and rep_id not in vistos:
                    vistos.add(rep_id)
                    grupos.setdefault((f["tarea"], f["variante"], "julios", "J", f["etiqueta"]), []).append(float(j))
    filas = ["| Tarea | Variante | Métrica | " + " | ".join(etiquetas) + " |",
             "|---|---|---|" + "---|" * len(etiquetas)]
    claves = sorted({k[:4] for k in grupos})
    for tarea, var, met, uni in claves:
        celdas, base = [], None
        for e in etiquetas:
            vs = grupos.get((tarea, var, met, uni, e))
            if not vs:
                celdas.append("—")
                continue
            m = statistics.fmean(vs)
            sd = statistics.stdev(vs) if len(vs) > 1 else 0.0
            dif = f" ({(m / base - 1) * 100:+.0f} %)" if base else ""
            base = base or m
            celdas.append(f"{m:.4g} ± {sd:.2g}{dif}")
        filas.append(f"| {tarea} | {var or '—'} | {met} ({uni}) | " + " | ".join(celdas) + " |")
    return "\n".join(filas)
