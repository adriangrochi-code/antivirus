"""adaptd: adaptación continua en tiempo real.

Cada pocos segundos lee la presión de CPU/memoria/E-S (PSI), predice la
evolución de la memoria disponible y detecta cargas especiales (juegos con
Proton/Wine, Waydroid). Según eso cambia entre estados y ajusta el sistema:

  normal   → perfil elegido por el usuario
  juego    → EPP/plataforma/potencia al máximo mientras haya un juego abierto
  presión  → memoria a punto de agotarse: reclaim anticipado y zram más agresivo
"""

from __future__ import annotations

import logging
import os
import re
import signal
import threading
from dataclasses import dataclass, field

from . import sysfs
from .apply import STATE_DIR, Actuator, apply_power, load_state, save_state
from .tuner import Plan

log = logging.getLogger("centinela.adaptd")

GAME_PROCESS = re.compile(
    r"(?i)(^|/)(wine(64)?(-preloader)?|wineserver|proton|pressure-vessel|reaper|"
    r"gamescope|[^/]+\.exe|steam_app_\d+)$")
ANDROID_PROCESS = re.compile(r"(?i)(waydroid|lxc-start)")


def read_psi(resource: str) -> dict[str, float]:
    """Devuelve {'some10': %, 'full10': %} de /proc/pressure/<resource>."""
    out = {}
    for line in (sysfs.read(f"/proc/pressure/{resource}", "") or "").splitlines():
        kind, *fields = line.split()
        for f in fields:
            k, _, v = f.partition("=")
            if k == "avg10":
                out[f"{kind}10"] = float(v)
    return out


def mem_available_mb() -> float:
    for line in (sysfs.read("/proc/meminfo", "") or "").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    return 0.0


class Holt:
    """Suavizado exponencial doble: nivel + tendencia, para predecir a corto plazo."""

    def __init__(self, alpha: float = 0.5, beta: float = 0.3):
        self.alpha, self.beta = alpha, beta
        self.level: float | None = None
        self.trend = 0.0

    def update(self, x: float) -> None:
        if self.level is None:
            self.level = x
            return
        prev = self.level
        self.level = self.alpha * x + (1 - self.alpha) * (self.level + self.trend)
        self.trend = self.beta * (self.level - prev) + (1 - self.beta) * self.trend

    def forecast(self, steps: float) -> float:
        return (self.level or 0.0) + self.trend * steps


_SEEN: dict[str, frozenset] = {}      # pid → cargas detectadas: cada proceso se examina UNA vez
_CALLS = 0


def _classify(pid: str) -> frozenset:
    comm = sysfs.read(f"/proc/{pid}/comm") or ""
    exe = ""
    try:
        exe = os.readlink(sysfs.path(f"/proc/{pid}/exe"))
    except OSError:
        pass
    cmd0 = (sysfs.read(f"/proc/{pid}/cmdline", "") or "").split("\0")[0]
    found = set()
    for s in (comm, exe, cmd0):
        if s and GAME_PROCESS.search(s.replace("\\", "/")):
            found.add("juego")
        if s and ANDROID_PROCESS.search(s):
            found.add("android")
    return frozenset(found)


def running_workloads() -> set[str]:
    """Juegos y Android en marcha. Antes se leían 3 archivos y un enlace de CADA proceso cada 3 s (~1.500
    aperturas con un escritorio normal); ahora solo los procesos nuevos (un juego cambia su nombre con exec
    pocas veces, y el siguiente proceso del lanzador ya se clasifica)."""
    global _CALLS
    _CALLS += 1
    if _CALLS % 10 == 0:
        _SEEN.clear()          # cada ~30 s todo de nuevo: un proceso que hizo exec a un juego (Wine) se detecta
    pids = [p for p in os.listdir(sysfs.path("/proc")) if p.isdigit()]
    live = set(pids)
    for gone in [p for p in _SEEN if p not in live]:
        del _SEEN[gone]
    found: set[str] = set()
    for pid in pids:
        cats = _SEEN.get(pid)
        if cats is None:
            cats = _SEEN[pid] = _classify(pid)
        found |= cats
    return found


@dataclass
class Decision:
    state: str
    reasons: list[str] = field(default_factory=list)


def decide(psi_mem: dict, psi_cpu: dict, avail_now: float, avail_pred: float,
           total_mb: float, workloads: set[str]) -> Decision:
    """Lógica pura de decisión (probada en tests)."""
    reasons = []
    low = max(total_mb * 0.05, 256)
    if psi_mem.get("full10", 0) > 5 or avail_pred < low or avail_now < low:
        reasons.append(f"memoria: disponible {avail_now:.0f} MB, prevista {avail_pred:.0f} MB, "
                       f"PSI full {psi_mem.get('full10', 0):.1f}%")
        return Decision("presion", reasons)
    if "juego" in workloads:
        reasons.append("juego detectado (Proton/Wine/gamescope)")
        return Decision("juego", reasons)
    if "android" in workloads and psi_cpu.get("some10", 0) > 20:
        reasons.append("Waydroid con CPU saturada")
        return Decision("juego", reasons)
    return Decision("normal", reasons)


class AdaptDaemon:
    def __init__(self, base_plan: Plan, interval: float = 3.0, horizon_s: float = 30.0,
                 dry_run: bool = False, allow_game_boost: bool = True, reload_plan=None):
        self.base = base_plan
        self.reload_plan = reload_plan
        self._prefs_mtime = self._mtime()
        self.interval = interval
        self.horizon_steps = horizon_s / interval
        self.act = Actuator(dry_run=dry_run)
        self.allow_game_boost = allow_game_boost
        self.mem = Holt()
        self.state = "normal"
        self._stop = threading.Event()
        from .memoria import Compactor
        from .modo_ia import Sistema
        self.compactor = Compactor()
        from .calor import Calor
        self.calor = Calor()
        self.modo_ia = Sistema()
        self._ticks = 0
        self.total_mb = mem_available_mb()
        for line in (sysfs.read("/proc/meminfo", "") or "").splitlines():
            if line.startswith("MemTotal:"):
                self.total_mb = int(line.split()[1]) / 1024

    def _boost_plan(self) -> Plan:
        """Perfil 'rendimiento' a partir del plan base (sin superar los máximos de fábrica)."""
        import copy
        p = copy.deepcopy(self.base)
        p.cpu = {"governor": p.cpu.get("governor"), "epp": "performance" if p.cpu.get("epp") else None}
        if self.base.power.get("platform_profile"):
            choices = (sysfs.read("/sys/firmware/acpi/platform_profile_choices", "") or "").split()
            p.power["platform_profile"] = "performance" if "performance" in choices else p.power["platform_profile"]
        return p

    def enter(self, state: str, reasons: list[str]) -> None:
        if state == self.state:
            return
        log.info("Estado %s → %s (%s)", self.state, state, "; ".join(reasons) or "carga normal")
        if state == "juego" and self.allow_game_boost:
            apply_power(self._boost_plan(), self.act)
            self.act.sysctl("vm.compaction_proactiveness", "0")  # sin tirones por compactación
        elif state == "presion":
            self.act.sysctl("vm.watermark_scale_factor", "200")  # reclaim antes
            self.act.sysctl("vm.compaction_proactiveness", "20")
        else:
            apply_power(self.base, self.act)
            self.act.sysctl("vm.watermark_scale_factor", self.base.sysctl.get("vm.watermark_scale_factor", "10"))
            self.act.sysctl("vm.compaction_proactiveness", "20")
        self.state = state

    def thermal_guard(self) -> None:
        """Si la GPU integrada con overclock supera 90 °C, se vuelve a reloj de fábrica."""
        for card in sysfs.ls("/sys/class/drm/card[0-9]"):
            od = sysfs.read(f"{card}/device/pp_sclk_od")
            if not od or od == "0":
                continue
            temps = [sysfs.read_int(f"{h}/temp1_input") or 0 for h in sysfs.ls(f"{card}/device/hwmon/hwmon*")]
            if temps and max(temps) >= 90_000:
                self.act.write(f"{card}/device/pp_sclk_od", "0")
                # Se recuerda: si no, el siguiente cambio de estado volvía a poner el overclock
                prefs = load_state("prefs.json") or {}
                prefs["gpu_oc"] = 0
                prefs.pop("autotune", None)
                save_state("prefs.json", prefs)
                log.warning("GPU a %.0f °C: overclock revertido a reloj de fábrica", max(temps) / 1000)

    @staticmethod
    def _mtime() -> float:
        try:
            return os.stat(os.path.join(STATE_DIR, "prefs.json")).st_mtime
        except OSError:
            return 0.0

    def _maybe_reload(self) -> None:
        m = self._mtime()
        if self.reload_plan and m != self._prefs_mtime:
            self._prefs_mtime = m
            try:
                self.base = self.reload_plan()
                log.info("Preferencias cambiadas: plan base %s / %s", self.base.power_profile, self.base.use_case)
            except Exception:  # noqa: BLE001
                log.exception("No se pudo recalcular el plan")

    def tick(self) -> Decision:
        self._maybe_reload()
        self.thermal_guard()
        try:
            self.calor.tick()
        except Exception:  # noqa: BLE001 — medir el calor nunca detiene la adaptación
            log.exception("Error midiendo la temperatura")
        avail = mem_available_mb()
        self.mem.update(avail)
        self._ticks += 1
        # Modo IA: con la IA local generando, ni KSM ni la compactación le quitan ancho de banda de memoria
        from . import modo_ia
        ia = modo_ia.activo()
        if ia and not self.modo_ia.pausado:
            log.info("Modo IA: la IA local está generando; pausadas la compactación y el escaneo de KSM")
            self.modo_ia.pausar()
        elif not ia and self.modo_ia.pausado:
            log.info("Modo IA terminado: se reanuda el mantenimiento de memoria")
            self.modo_ia.reanudar()
        if self._ticks % 20 == 0 and not ia:      # cada ~minuto: comprimir la memoria fría de apps sin usar
            try:
                self.compactor.step(avail, self.total_mb)
            except Exception:  # noqa: BLE001 — una pasada fallida no detiene la adaptación
                log.exception("Error compactando apps en segundo plano")
        d = decide(read_psi("memory"), read_psi("cpu"), avail,
                   self.mem.forecast(self.horizon_steps), self.total_mb, running_workloads())
        self.enter(d.state, d.reasons)
        return d

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        log.info("adaptd activo: perfil %s / uso %s", self.base.power_profile, self.base.use_case)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — el demonio nunca debe morir
                log.exception("Error en ciclo de adaptación")
            self._stop.wait(self.interval)
        self.modo_ia.reanudar()
        try:
            self.calor.guardar()
        except OSError:
            pass
        self.enter("normal", ["apagando"])
