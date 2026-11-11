"""Autoajuste: overclock y undervolt automáticos con pruebas de estabilidad internas.

Un botón («Optimizar automáticamente») hace todo:
1. Detecta qué permite el hardware (overclock de GPU por overdrive, undervolt de GPU por curva o
   desfase de voltaje, Turbo de CPU). El undervolt de CPU Intel no se ofrece: requiere escribir
   MSR, que el lockdown del kernel bloquea a propósito.
2. Busca paso a paso: aplica un paso, ejecuta la prueba de estabilidad (cálculo verificado en CPU y
   memoria con centinela-estres + carga de GPU + vigilancia del registro del kernel y la
   temperatura). Al primer fallo vuelve al último paso estable y se aleja UN paso más como margen.
3. Protección ante cuelgues: antes de cada paso se escribe una marca «en prueba». Si el equipo se
   cuelga y reinicia, el servicio de arranque ve la marca, deja todo de fábrica y anota el valor
   como inestable (no se volverá a probar).
4. Guarda el perfil estable y lo reaplica en cada arranque.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import sysfs
from .apply import OC_MAX, Actuator, load_state, save_state

STATE_DIR = Path(os.environ.get("CENTINELA_STATE", "/var/lib/centinela"))
MARKER = STATE_DIR / "autotune-en-prueba.json"
PROGRESS = Path(os.environ.get("CENTINELA_PROGRESS", "/run/centinela/autotune.json"))
ESTRES = next((p for p in ("/usr/lib/centinela/centinela-estres",
                           str(Path(__file__).resolve().parents[1] / "build" / "centinela-estres"))
               if os.access(p, os.X_OK)), "centinela-estres")
UV_MIN_MV = -100            # undervolt máximo que se prueba
TEMP_LIMIT = 90.0
KERNEL_ERRORS = re.compile(r"(amdgpu.*(ring \S+ timeout|GPU reset|VM_L2_PROTECTION_FAULT|failed to)|"
                           r"\bmce:|Machine check|Hardware Error|soft lockup|hard LOCKUP|nouveau.*(fault|timeout)|"
                           r"i915.*(GPU HANG|reset)|xe.*(hang|reset))", re.I)


# ---------------------------------------------------------------- capacidades
def gpu_cards() -> list[str]:
    return [c for c in sysfs.ls("/sys/class/drm/card[0-9]") if sysfs.read(f"{c}/device/pp_sclk_od") is not None
            or sysfs.read(f"{c}/device/pp_od_clk_voltage") is not None]


def uv_method(card: str) -> str | None:
    """'offset' (RDNA: OD_VDDGFX_OFFSET), 'curve' (Vega/Polaris: niveles con mV) o None."""
    table = sysfs.read(f"{card}/device/pp_od_clk_voltage") or ""
    if "OD_VDDGFX_OFFSET" in table:
        return "offset"
    if re.search(r"OD_SCLK:\s*\n\s*\d+:\s*\d+Mhz\s+\d+mV", table, re.I):
        return "curve"
    return None


def capabilities() -> dict:
    cards = gpu_cards()
    return {"gpu_oc": [c for c in cards if sysfs.read(f"{c}/device/pp_sclk_od") is not None],
            "gpu_uv": {c: m for c in cards if (m := uv_method(c))},
            "turbo": sysfs.read("/sys/devices/system/cpu/cpufreq/boost") is not None,
            "cpu_uv": False,
            "cpu_uv_motivo": ("El undervolt de CPU necesita escribir registros MSR (Intel) o la SMU (AMD), que el "
                              "lockdown del kernel bloquea para proteger el sistema; no se automatiza.")}


# ---------------------------------------------------------------- aplicar
def set_gpu_oc(act: Actuator, card: str, pct: int) -> None:
    act.write(f"{card}/device/pp_sclk_od", str(max(0, min(OC_MAX, int(pct)))))


def stock_curve(card: str, reset: bool = False, dry_run: bool = False) -> list[tuple[int, int, int]]:
    """Curva de reloj/voltaje. Con reset=True se restaura antes la de fábrica: si no, se leía la ya rebajada y
    cada aplicación restaba otros N mV (el undervolt se acumulaba hasta el mínimo y colgaba la GPU)."""
    if reset and not dry_run:
        for val in ("r", "c"):
            try:
                with open(sysfs.path(f"{card}/device/pp_od_clk_voltage"), "w") as fh:
                    fh.write(val + "\n")
            except OSError:
                pass
    table = sysfs.read(f"{card}/device/pp_od_clk_voltage") or ""
    sec = table.split("OD_SCLK:")[1].split("OD_")[0] if "OD_SCLK:" in table else ""
    return [(int(a), int(b), int(c)) for a, b, c in re.findall(r"(\d+):\s*(\d+)Mhz\s+(\d+)mV", sec, re.I)]


def set_gpu_uv(card: str, method: str, mv: int, base: list | None = None, dry_run: bool = False) -> list[str]:
    """Escribe el undervolt (mv <= 0) por el interfaz de overdrive y lo confirma con «c»."""
    mv = max(UV_MIN_MV, min(0, int(mv)))
    cmds = [f"vo {mv}"] if method == "offset" else \
        [f"s {lvl} {mhz} {max(700, volt + mv)}" for lvl, mhz, volt in (base or [])[-3:]]
    cmds.append("c")
    if not Actuator.allowed(f"{card}/device/pp_od_clk_voltage"):
        raise PermissionError("ruta fuera de la lista blanca")
    if not dry_run:
        path = sysfs.path(f"{card}/device/pp_od_clk_voltage")
        for c in cmds:
            with open(path, "w") as fh:
                fh.write(c + "\n")
    return cmds


def reset_gpu(card: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    for path, val in ((f"{card}/device/pp_sclk_od", "0"), (f"{card}/device/pp_od_clk_voltage", "r"),
                      (f"{card}/device/pp_od_clk_voltage", "c")):
        try:
            with open(sysfs.path(path), "w") as fh:
                fh.write(val + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------- prueba de estabilidad
def max_temp() -> float:
    vals = [sysfs.read_int(t) or 0 for h in sysfs.ls("/sys/class/hwmon/hwmon*") for t in sysfs.ls(f"{h}/temp*_input")]
    return max(vals, default=0) / 1000


class KernelLog:
    """Lee los mensajes nuevos del kernel (/dev/kmsg) desde el inicio de la prueba."""

    def __init__(self):
        try:
            self.fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
            os.lseek(self.fd, 0, os.SEEK_END)
        except OSError:
            self.fd = None

    def errors(self) -> list[str]:
        out = []
        while self.fd is not None:
            try:
                rec = os.read(self.fd, 8192).decode("utf-8", "replace")
            except BlockingIOError:
                break
            except OSError:
                continue          # EPIPE: se sobrescribieron mensajes; seguir
            msg = rec.split(";", 1)[-1]
            if KERNEL_ERRORS.search(msg):
                out.append(msg.strip()[:200])
        return out

    def close(self):
        if self.fd is not None:
            os.close(self.fd)


def gpu_load_cmd() -> list[str] | None:
    """Carga de GPU en la sesión gráfica del usuario que pidió el ajuste (pkexec define PKEXEC_UID)."""
    uid = os.environ.get("PKEXEC_UID")
    exe = next((shutil.which(x) for x in ("glmark2-es2-wayland", "glmark2-wayland", "vkmark", "glmark2") if shutil.which(x)), None)
    if not (uid and exe and shutil.which("runuser")):
        return None
    rt = f"/run/user/{uid}"
    user = subprocess.run(["id", "-nu", uid], capture_output=True, text=True).stdout.strip()
    extra = ["--run-forever"] if "glmark2" in exe else []
    return ["runuser", "-u", user, "--", "env", f"XDG_RUNTIME_DIR={rt}", "WAYLAND_DISPLAY=wayland-0", exe, *extra]


@dataclass
class StabilityResult:
    ok: bool
    motivo: str = ""
    temp_max: float = 0.0
    detalle: dict = field(default_factory=dict)


def stability_test(seconds: int = 45) -> StabilityResult:
    klog = KernelLog()
    gpu = None
    cmd = gpu_load_cmd()
    if cmd:
        gpu = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    stress = subprocess.Popen([ESTRES, "todo", str(seconds)], stdout=subprocess.PIPE, text=True)
    tmax, motivo = 0.0, ""
    try:
        while stress.poll() is None:
            t = max_temp()
            tmax = max(tmax, t)
            if t >= TEMP_LIMIT:
                motivo = f"temperatura {t:.0f} °C"
                stress.kill()
                break
            errs = klog.errors()
            if errs:
                motivo = "error del kernel: " + errs[0]
                stress.kill()
                break
            if gpu and gpu.poll() is not None:
                # Sin carga de GPU la prueba solo validaría la CPU: se da por no superada (lo seguro)
                motivo = "la carga de GPU (glmark2) se cerró: no se pudo probar la GPU"
                stress.kill()
                break
            time.sleep(1)
        out = stress.communicate()[0] or "{}"
    finally:
        if gpu:
            os.killpg(gpu.pid, signal.SIGTERM)
        klog.close()
    try:
        res = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {}
    if not motivo and not res.get("ok"):
        motivo = ("no había memoria libre para la prueba" if res.get("sin_memoria")
                  else f"{res.get('errores', '?')} errores de cálculo/memoria" if res else "la prueba no terminó")
    return StabilityResult(not motivo, motivo, tmax, {**res, "carga_gpu": bool(cmd)})


# ---------------------------------------------------------------- búsqueda
def search(steps: list[int], apply, test, progress=lambda *_: None, known_bad: set | None = None,
           before=lambda v: None) -> tuple[int, list]:
    """Prueba los pasos en orden; devuelve (valor estable con margen, historial).
    El valor estable es el anterior al último que pasó si hubo un fallo (margen de un paso)."""
    history, stable = [], 0
    passed = [0]
    for v in steps:
        if known_bad and v in known_bad:
            history.append({"valor": v, "ok": False, "motivo": "inestable en un intento anterior"})
            break
        progress(v)
        before(v)                       # la marca «en prueba» va ANTES de aplicar: un cuelgue al aplicar cuenta
        apply(v)
        r = test(v)
        history.append({"valor": v, "ok": r.ok, "motivo": r.motivo, "temp_max": r.temp_max})
        if not r.ok:
            break
        passed.append(v)
    else:
        return passed[-1], history          # todos los pasos estables: sin necesidad de margen
    stable = passed[-2] if len(passed) >= 2 else 0
    apply(stable)
    return stable, history


# ---------------------------------------------------------------- orquestación
def write_progress(data: dict) -> None:
    try:
        PROGRESS.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROGRESS.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        os.chmod(tmp, 0o644)
        os.replace(tmp, PROGRESS)
    except OSError:
        pass


def run(seconds: int = 45, dry_run: bool = False, test=stability_test) -> dict:
    caps = capabilities()
    act = Actuator(dry_run=dry_run)
    bad = (load_state("autotune-inestables.json") or {})
    report = {"inicio": time.time(), "capacidades": {k: v for k, v in caps.items() if k != "cpu_uv_motivo"},
              "aviso_cpu_uv": caps["cpu_uv_motivo"], "pasos": [], "resultado": {}, "estado": "en curso"}

    def mark(knob, value):
        if not dry_run:
            MARKER.parent.mkdir(parents=True, exist_ok=True)
            MARKER.write_text(json.dumps({"ajuste": knob, "valor": value, "t": time.time()}))

    def tested(knob):
        def t(v):
            report["fase"] = f"{knob} {v}"
            write_progress(report)
            r = test(seconds)
            report["pasos"].append({"ajuste": knob, "valor": v, "ok": r.ok, "motivo": r.motivo,
                                    "temp_max": r.temp_max, "detalle": r.detalle})
            write_progress(report)
            return r
        return t

    try:
        # 1) Modo OC: Turbo encendido
        if caps["turbo"]:
            act.write("/sys/devices/system/cpu/cpufreq/boost", "1")
            report["resultado"]["turbo"] = True
        # 2) Overclock de GPU por pasos de 2 %
        for card in caps["gpu_oc"]:
            name = card.rsplit("/", 1)[-1]
            knob = f"oc {name}"
            oc, _ = search(list(range(2, OC_MAX + 1, 2)) + ([OC_MAX] if OC_MAX % 2 else []),
                           lambda v, c=card: set_gpu_oc(act, c, v), tested(knob),
                           known_bad=set(bad.get(knob, [])), before=lambda v, k=knob: mark(k, v))
            report["resultado"][f"gpu_oc_{name}"] = oc
        # 3) Undervolt de GPU por pasos de 10 mV (con el overclock ya fijado), siempre desde la curva de fábrica
        for card, method in caps["gpu_uv"].items():
            name = card.rsplit("/", 1)[-1]
            knob = f"uv {name}"
            base = None
            if method == "curve":
                base = stock_curve(card, reset=True, dry_run=dry_run)
                oc = report["resultado"].get(f"gpu_oc_{name}")
                if oc:
                    set_gpu_oc(act, card, oc)          # el reset de la tabla puede deshacer el overclock
            uv, _ = search(list(range(-10, UV_MIN_MV - 1, -10)),
                           lambda v, c=card, m=method, b=base: set_gpu_uv(c, m, v, b, dry_run), tested(knob),
                           known_bad=set(bad.get(knob, [])), before=lambda v, k=knob: mark(k, v))
            report["resultado"][f"gpu_uv_mv_{name}"] = uv
    except BaseException:
        # Error, falta de centinela-estres o Ctrl+C: nunca dejar aplicado un paso sin probar
        for c in gpu_cards():
            reset_gpu(c, dry_run)
        if not dry_run and MARKER.exists():
            MARKER.unlink()
        report["estado"] = "interrumpido: valores de fábrica restaurados"
        write_progress(report)
        raise
    if not dry_run and MARKER.exists():
        MARKER.unlink()
    report["estado"] = "terminado"
    report["fin"] = time.time()
    write_progress(report)
    if not dry_run:
        prefs = load_state("prefs.json") or {}
        prefs["autotune"] = report["resultado"]
        save_state("prefs.json", prefs)
        save_state("autotune-ultimo.json", report)
    return report


def boot_apply(dry_run: bool = False) -> str:
    """Al arrancar: si quedó una marca «en prueba», el equipo se colgó probando → fábrica + anotar."""
    if MARKER.exists():
        try:
            m = json.loads(MARKER.read_text())
        except (OSError, ValueError):
            m = {}
        bad = load_state("autotune-inestables.json") or {}
        if m.get("ajuste"):
            bad.setdefault(m["ajuste"], []).append(m.get("valor"))
            save_state("autotune-inestables.json", bad)
        for c in gpu_cards():
            reset_gpu(c, dry_run)
        MARKER.unlink()
        prefs = load_state("prefs.json") or {}
        prefs.pop("autotune", None)
        save_state("prefs.json", prefs)
        return f"El equipo se colgó probando {m.get('ajuste')} = {m.get('valor')}: valores de fábrica restaurados"
    res = (load_state("prefs.json") or {}).get("autotune") or {}
    act = Actuator(dry_run=dry_run)
    for c in gpu_cards():
        name = c.rsplit("/", 1)[-1]
        if f"gpu_oc_{name}" in res:
            set_gpu_oc(act, c, res[f"gpu_oc_{name}"])
        m = uv_method(c)
        if m and res.get(f"gpu_uv_mv_{name}"):
            base = stock_curve(c, reset=True, dry_run=dry_run) if m == "curve" else None
            if m == "curve" and f"gpu_oc_{name}" in res:
                set_gpu_oc(act, c, res[f"gpu_oc_{name}"])
            set_gpu_uv(c, m, res[f"gpu_uv_mv_{name}"], base, dry_run)
    return "Perfil estable aplicado" if res else "Sin perfil automático"
