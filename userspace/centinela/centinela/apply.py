"""Aplica un plan: archivos de configuración persistentes y ajustes en caliente."""

from __future__ import annotations

import fnmatch
import json
import logging
import os

from . import sysfs
from .tuner import Plan

log = logging.getLogger("centinela.apply")

ETC = os.environ.get("CENTINELA_ETC", "/etc")
STATE_DIR = os.environ.get("CENTINELA_STATE", "/var/lib/centinela")

# Únicas rutas de /proc y /sys en las que Centinela puede escribir.
VM_KEYS = ["swappiness", "page-cluster", "watermark_boost_factor", "watermark_scale_factor",
           "vfs_cache_pressure", "max_map_count", "compaction_proactiveness"]
WRITABLE = [
    *(f"/proc/sys/vm/{k.replace('-', '_')}" for k in VM_KEYS), "/proc/sys/vm/page-cluster",
    "/proc/sys/kernel/split_lock_mitigate",
    "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
    "/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference",
    "/sys/firmware/acpi/platform_profile",
    "/sys/class/powercap/intel-rapl:*/constraint_*_power_limit_uw",
    "/sys/block/*/queue/scheduler",
    "/sys/block/*/queue/read_ahead_kb",
    "/sys/module/zswap/parameters/*",
    "/sys/class/drm/card*/device/hwmon/hwmon*/power1_cap",
    "/sys/kernel/mm/transparent_hugepage/enabled",
    "/sys/kernel/mm/transparent_hugepage/defrag",
    "/sys/devices/system/cpu/cpufreq/boost",
    "/sys/class/drm/card*/device/pp_sclk_od",
    "/sys/class/drm/card*/device/pp_od_clk_voltage",
    "/sys/module/damon_reclaim/parameters/*",
    # energía adaptativa (centinela energia)
    "/sys/class/scsi_host/host*/link_power_management_policy",
    "/sys/module/pcie_aspm/parameters/policy",
    "/sys/bus/pci/devices/*/power/control",
    "/sys/bus/usb/devices/*/power/control",
    "/sys/module/snd_hda_intel/parameters/power_save",
    "/proc/sys/vm/dirty_writeback_centisecs", "/proc/sys/vm/stat_interval",
    "/sys/class/power_supply/BAT*/charge_control_start_threshold",
    "/sys/class/power_supply/BAT*/charge_control_end_threshold",
]
OC_MAX = 15               # % máximo de overclock de la GPU integrada que centinela acepta


class Actuator:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.changes: list[tuple[str, str]] = []

    @staticmethod
    def allowed(p: str) -> bool:
        """Coincidencia por segmentos: «*» nunca cruza «/» (fnmatch sí lo haría)."""
        norm = os.path.normpath(p)
        if ".." in p.split("/"):
            return False
        segs = norm.split("/")
        for w in WRITABLE:
            ws = w.split("/")
            if len(ws) == len(segs) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(segs, ws)):
                return True
        return False

    def write(self, p: str, value: str) -> bool:
        if not self.allowed(p):
            raise PermissionError(f"Ruta fuera de la lista blanca: {p}")
        if sysfs.read(p) == str(value):
            return False
        if self.dry_run:
            self.changes.append((p, str(value)))
            log.info("[dry-run] %s <- %s", p, value)
            return True
        try:
            with open(sysfs.path(p), "w", encoding="utf-8") as fh:
                fh.write(str(value))
            self.changes.append((p, str(value)))   # solo lo que de verdad se escribió
            log.info("%s <- %s", p, value)
            return True
        except OSError as e:
            log.warning("No se pudo escribir %s: %s", p, e)
            return False

    def sysctl(self, key: str, value: str) -> bool:
        return self.write("/proc/sys/" + key.replace(".", "/"), value)


def _write_file(rel: str, content: str, dry_run: bool) -> str:
    path = os.path.join(ETC, rel)
    if dry_run:
        log.info("[dry-run] escribiría %s", path)
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


HEADER = "# Generado por centinela — no editar a mano (ver `centinela plan`)\n"


def render_files(plan: Plan) -> dict[str, str]:
    files = {}
    files["sysctl.d/60-centinela-tuned.conf"] = HEADER + "".join(
        f"{k} = {v}\n" for k, v in sorted(plan.sysctl.items()))
    rules = [HEADER]
    for dev, s in plan.io_schedulers.items():
        rules.append(f'ACTION=="add|change", KERNEL=="{dev}", ATTR{{queue/scheduler}}="{s["scheduler"]}", '
                     f'ATTR{{queue/read_ahead_kb}}="{s["read_ahead_kb"]}"\n')
    files["udev/rules.d/60-centinela-io.rules"] = "".join(rules)
    if plan.zram:
        files["systemd/zram-generator.conf"] = HEADER + "[zram0]\n" + "".join(
            f"{k} = {v}\n" for k, v in plan.zram.items())
    files["default/grub.d/60-centinela-tuned.cfg"] = (
        HEADER + f'GRUB_CMDLINE_LINUX_DEFAULT="quiet splash {" ".join(plan.cmdline)}"\n')
    files["initramfs-tools/conf.d/60-centinela-tuned.conf"] = (
        HEADER + f"MODULES=dep\nCOMPRESS={plan.initramfs_compress}\n")
    files["centinela/hardware.config"] = "\n".join(plan.kernel_fragment) + "\n"
    files["centinela/builtin-modules.txt"] = "\n".join(plan.builtin_modules) + "\n"
    files["centinela/plan.json"] = json.dumps(plan.to_dict(), indent=2, ensure_ascii=False)
    if plan.oomd:
        files["systemd/oomd.conf.d/60-centinela.conf"] = HEADER + "[OOM]\n" + "".join(
            f"{k}={v}\n" for k, v in plan.oomd.items())
    if plan.threads:
        # Lectura pública: TISMA Code (usuario) toma de aquí los hilos de llama.cpp.
        files["centinela/hilos.json"] = json.dumps(plan.threads, indent=2)
    return files


LIGHT = {"blurEnabled": "false", "contrastEnabled": "false", "translucencyEnabled": "false"}


def light_kwinrc(current: str) -> str:
    """kwinrc con los efectos que más ancho de banda gastan desactivados (se conserva el resto)."""
    lines, in_plugins, done = [], False, set()
    for line in current.splitlines():
        if line.startswith("["):
            if in_plugins:
                lines += [f"{k}={v}" for k, v in LIGHT.items() if k not in done]
            in_plugins = line.strip() == "[Plugins]"
        key = line.split("=", 1)[0].strip()
        if in_plugins and key in LIGHT:
            lines.append(f"{key}={LIGHT[key]}")
            done.add(key)
            continue
        lines.append(line)
    if in_plugins:
        lines += [f"{k}={v}" for k, v in LIGHT.items() if k not in done]
    if "[Plugins]" not in current:
        lines += ["", "[Plugins]"] + [f"{k}={v}" for k, v in LIGHT.items()]
    return "\n".join(lines) + "\n"


def persist(plan: Plan, dry_run: bool = False) -> list[str]:
    files = render_files(plan)
    if plan.desktop_light:
        try:
            cur = open(os.path.join(ETC, "xdg/kwinrc"), encoding="utf-8").read()
        except OSError:
            cur = ""
        files["xdg/kwinrc"] = light_kwinrc(cur)
    out = []
    for rel, content in files.items():
        try:
            out.append(_write_file(rel, content, dry_run))
        except OSError as e:           # uno que falle (p. ej. solo lectura) no debe impedir aplicar el resto
            log.warning("No se pudo escribir %s: %s", rel, e)
    return out


def apply_runtime(plan: Plan, act: Actuator) -> None:
    for k, v in plan.sysctl.items():
        try:
            act.sysctl(k, v)
        except PermissionError as e:
            log.warning("%s", e)
    for dev, s in plan.io_schedulers.items():
        act.write(f"/sys/block/{dev}/queue/scheduler", s["scheduler"])
        act.write(f"/sys/block/{dev}/queue/read_ahead_kb", str(s["read_ahead_kb"]))
    if plan.zswap:
        act.write("/sys/module/zswap/parameters/compressor", plan.zswap["compressor"])
        act.write("/sys/module/zswap/parameters/max_pool_percent", str(plan.zswap["max_pool_percent"]))
        act.write("/sys/module/zswap/parameters/enabled", "Y")
    apply_damon(plan, act)
    apply_power(plan, act)


def apply_damon(plan: Plan, act: Actuator) -> None:
    """Los parámetros se escriben con DAMON parado o se confirman con commit_inputs (la documentación del kernel
    prohíbe escribir otros parámetros mientras commit_inputs sigue en Y)."""
    base = "/sys/module/damon_reclaim/parameters"
    if not plan.damon or sysfs.read(f"{base}/enabled") is None:
        return
    running = sysfs.read(f"{base}/enabled") == "Y"
    for k, v in plan.damon.items():
        if k != "enabled":
            act.write(f"{base}/{k}", v)
    if running:
        if act.changes and any(p.startswith(base) for p, _ in act.changes):
            act.write(f"{base}/commit_inputs", "Y")
    else:
        act.write(f"{base}/enabled", plan.damon.get("enabled", "Y"))


def apply_power(plan: Plan, act: Actuator) -> None:
    for cpu in sysfs.ls("/sys/devices/system/cpu/cpu[0-9]*"):
        if plan.cpu.get("governor"):
            act.write(f"{cpu}/cpufreq/scaling_governor", plan.cpu["governor"])
        if plan.cpu.get("epp"):
            act.write(f"{cpu}/cpufreq/energy_performance_preference", plan.cpu["epp"])
    pw = plan.power
    if pw.get("platform_profile"):
        act.write("/sys/firmware/acpi/platform_profile", pw["platform_profile"])
    for key, r in pw.get("rapl", {}).items():
        zone, idx = key.rsplit(":", 1)
        act.write(f"{zone}/constraint_{idx}_power_limit_uw", str(int(r["target_w"] * 1e6)))
    for hwmon, r in pw.get("gpu_power_caps", {}).items():
        act.write(f"{hwmon}/power1_cap", str(int(r["target_w"] * 1e6)))
    # Core Performance Boost: activo salvo en silencioso
    act.write("/sys/devices/system/cpu/cpufreq/boost", "0" if plan.power_profile == "silencioso" else "1")
    prefs = load_state("prefs.json") or {}
    if prefs.get("autotune"):
        return                       # el perfil del autoajuste (probado) manda sobre el del plan
    # La elección del usuario (o la reversión por temperatura) manda sobre la sugerencia del plan
    oc = max(0, min(OC_MAX, int(prefs["gpu_oc"] if "gpu_oc" in prefs else (getattr(plan, "gpu_oc_percent", 0) or 0))))
    for card in sysfs.ls("/sys/class/drm/card[0-9]"):
        if sysfs.read(f"{card}/device/pp_sclk_od") is not None:
            act.write(f"{card}/device/pp_sclk_od", str(oc))


def save_state(name: str, data: dict) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    p = os.path.join(STATE_DIR, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return p


def load_state(name: str) -> dict | None:
    try:
        with open(os.path.join(STATE_DIR, name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
