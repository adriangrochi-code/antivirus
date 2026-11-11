"""Estado en vivo del equipo, legible sin privilegios (para la bandeja y TISMA Rendimiento)."""

from __future__ import annotations

import json
import re
import time

from . import sysfs


def _cpu_times() -> tuple[int, int]:
    f = (sysfs.read("/proc/stat", "") or "").splitlines()[0].split()[1:]
    vals = [int(x) for x in f]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def cpu_usage(interval: float = 0.2) -> float:
    t1, i1 = _cpu_times()
    time.sleep(interval)
    t2, i2 = _cpu_times()
    if t2 <= t1:                      # sin ticks nuevos: no hay datos, no «100 %»
        return 0.0
    return round(max(0.0, min(100.0, 100 * (1 - (i2 - i1) / (t2 - t1)))), 1)


def cpu_freqs() -> list[int]:
    out = []
    for c in sysfs.ls("/sys/devices/system/cpu/cpu[0-9]*"):
        f = sysfs.read_int(f"{c}/cpufreq/scaling_cur_freq")
        if f:
            out.append(f // 1000)
    return out


def temps() -> dict[str, float]:
    """Temperaturas por sensor conocido (k10temp/zenpower = CPU AMD, coretemp = Intel, amdgpu…)."""
    out = {}
    for h in sysfs.ls("/sys/class/hwmon/hwmon*"):
        name = sysfs.read(f"{h}/name") or ""
        vals = [sysfs.read_int(t) for t in sysfs.ls(f"{h}/temp*_input")]
        vals = [v for v in vals if v]
        if not vals:
            continue
        label = {"k10temp": "cpu", "zenpower": "cpu", "coretemp": "cpu", "amdgpu": "gpu", "nvme": "ssd",
                 "acpitz": "placa", "thinkpad": "placa"}.get(name, name)
        out[label] = max(out.get(label, 0), round(max(vals) / 1000, 1))
    return out


def memory() -> dict:
    mi = {k: int(v) for k, v in re.findall(r"^(\w+):\s+(\d+)", sysfs.read("/proc/meminfo", "") or "", re.M)}
    total = mi.get("MemTotal", 0) // 1024
    avail = mi.get("MemAvailable", 0) // 1024
    out = {"total_mb": total, "used_mb": total - avail, "available_mb": avail,
           "swap_used_mb": (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) // 1024}
    mm = (sysfs.read("/sys/block/zram0/mm_stat") or "").split()
    if len(mm) >= 3 and int(mm[1]) > 0:
        out["zram"] = {"datos_mb": int(mm[0]) // (1 << 20), "comprimido_mb": int(mm[1]) // (1 << 20),
                       "ratio": round(int(mm[0]) / int(mm[1]), 2),
                       "algoritmo": re.sub(r".*\[(\w+)\].*", r"\1", sysfs.read("/sys/block/zram0/comp_algorithm", "") or "")}
    return out


def gpus() -> list[dict]:
    out = []
    for card in sysfs.ls("/sys/class/drm/card[0-9]"):
        dev = f"{card}/device"
        busy = sysfs.read_int(f"{dev}/gpu_busy_percent")
        if busy is None and sysfs.read(f"{dev}/pp_dpm_sclk") is None:
            continue
        sclk = re.search(r"(\d+)Mhz \*", sysfs.read(f"{dev}/pp_dpm_sclk", "") or "")
        levels = [int(x) for x in re.findall(r"(\d+)Mhz", sysfs.read(f"{dev}/pp_dpm_sclk", "") or "")]
        out.append({"card": card.rsplit("/", 1)[-1], "uso": busy, "mhz": int(sclk.group(1)) if sclk else None,
                    "mhz_max": max(levels) if levels else None,
                    "vram_usada_mb": (sysfs.read_int(f"{dev}/mem_info_vram_used") or 0) >> 20,
                    "vram_total_mb": (sysfs.read_int(f"{dev}/mem_info_vram_total") or 0) >> 20,
                    "gtt_usada_mb": (sysfs.read_int(f"{dev}/mem_info_gtt_used") or 0) >> 20,
                    "oc_porcentaje": sysfs.read_int(f"{dev}/pp_sclk_od"),
                    "oc_disponible": sysfs.read(f"{dev}/pp_sclk_od") is not None,
                    "nivel": sysfs.read(f"{dev}/power_dpm_force_performance_level")})
    return out


def psi() -> dict:
    out = {}
    for r in ("cpu", "memory", "io"):
        m = re.search(r"some avg10=([\d.]+)", sysfs.read(f"/proc/pressure/{r}", "") or "")
        out[r] = float(m.group(1)) if m else None
    return out


def prefs() -> dict:
    try:
        return json.loads(open(sysfs.path("/var/lib/centinela/prefs.json")).read())
    except (OSError, ValueError):
        return {}


def snapshot() -> dict:
    freqs = cpu_freqs()
    return {"t": time.time(), "cpu": {"uso": cpu_usage(), "mhz_media": round(sum(freqs) / len(freqs)) if freqs else None,
                                      "mhz_max": max(freqs) if freqs else None,
                                      "turbo": sysfs.read("/sys/devices/system/cpu/cpufreq/boost"),
                                      "gobernador": sysfs.read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")},
            "temperaturas": temps(), "memoria": memory(), "gpus": gpus(), "presion": psi(),
            "perfil": prefs().get("perfil", "equilibrado"),
            "plataforma": sysfs.read("/sys/firmware/acpi/platform_profile")}
