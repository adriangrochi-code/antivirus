"""Detección del hardware: CPU, memoria, GPUs, almacenamiento y potencia."""

from __future__ import annotations

import os
import re

from . import sysfs

GPU_VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}
LAPTOP_CHASSIS = {"8", "9", "10", "14", "30", "31", "32"}
# GT/s por línea de PCIe y eficiencia de codificación
PCIE_GTS = {2.5: 0.8, 5.0: 0.8, 8.0: 128 / 130, 16.0: 128 / 130, 32.0: 128 / 130, 64.0: 242 / 256}
INTERESTING_FLAGS = ["avx2", "avx512f", "avx512_vnni", "avx_vnni", "amx_tile", "sha_ni",
                     "aes", "vaes", "bmi2", "fma", "x2apic", "hypervisor"]


def parse_cpulist(s: str | None) -> list[int]:
    out: list[int] = []
    for part in (s or "").split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return out


def pcie_gbs(speed: str | None, width: str | int | None) -> float | None:
    """Ancho de banda teórico de un enlace PCIe en GB/s por sentido."""
    if not speed or not width:
        return None
    m = re.match(r"([\d.]+)\s*GT/s", speed)
    if not m:
        return None
    gts = float(m.group(1))
    eff = PCIE_GTS.get(gts, 128 / 130)
    return round(gts * eff * int(width) / 8, 2)


# ---------------------------------------------------------------- CPU
def cpu() -> dict:
    info: dict = {}
    text = sysfs.read("/proc/cpuinfo", "") or ""
    first = text.split("\n\n")[0]
    for line in first.splitlines():
        if ":" in line:
            k, v = (x.strip() for x in line.split(":", 1))
            info[k] = v
    flags = set(info.get("flags", "").split())
    cpus = parse_cpulist(sysfs.read("/sys/devices/system/cpu/online", "0"))

    # Frecuencia máxima por CPU: agrupa núcleos de rendimiento (P) y eficiencia (E)
    max_freq: dict[int, int] = {}
    for c in cpus:
        f = sysfs.read_int(f"/sys/devices/system/cpu/cpu{c}/cpufreq/cpuinfo_max_freq")
        if f:
            max_freq[c] = f
    hybrid = {}
    for kind in ("cpu_core", "cpu_atom"):
        lst = sysfs.read(f"/sys/devices/{kind}/cpus")
        if lst:
            hybrid["P" if kind == "cpu_core" else "E"] = parse_cpulist(lst)

    # Dominios de L3 compartida (CCDs en AMD, clusters en Intel)
    l3: dict[str, list[int]] = {}
    for c in cpus:
        for idx in sysfs.ls(f"/sys/devices/system/cpu/cpu{c}/cache/index*"):
            if sysfs.read(f"{idx}/level") == "3":
                shared = sysfs.read(f"{idx}/shared_cpu_list", "")
                l3.setdefault(shared, parse_cpulist(shared))
    caches = {}
    for idx in sysfs.ls("/sys/devices/system/cpu/cpu0/cache/index*"):
        lvl, typ = sysfs.read(f"{idx}/level"), sysfs.read(f"{idx}/type")
        caches[f"L{lvl}{'d' if typ == 'Data' else 'i' if typ == 'Instruction' else ''}"] = sysfs.read(f"{idx}/size")

    # Unidades de ejecución: CPUs que comparten núcleo (SMT) o, en AMD familia 15h (Bulldozer…Excavator),
    # módulo CMT: dos núcleos enteros con FPU y L2 compartidas. Linux los presenta igual (thread_siblings).
    units = sorted({sysfs.read(f"/sys/devices/system/cpu/cpu{c}/topology/thread_siblings_list") or str(c)
                    for c in cpus}, key=lambda s: parse_cpulist(s)[:1])
    cmt = info.get("vendor_id") == "AuthenticAMD" and info.get("cpu family") == "21"
    smt = sysfs.read("/sys/devices/system/cpu/smt/active")
    driver = sysfs.read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver")
    return {
        "vendor": info.get("vendor_id"),
        "model": info.get("model name"),
        "family": info.get("cpu family"),
        "model_id": info.get("model"),
        "logical_cpus": len(cpus),
        "physical_cores": len({sysfs.read(f"/sys/devices/system/cpu/cpu{c}/topology/core_cpus_list") for c in cpus}),
        "smt": smt == "1" and not cmt,
        "cmt": cmt,
        "execution_units": [parse_cpulist(u) for u in units],
        "flags": sorted(f for f in INTERESTING_FLAGS if f in flags),
        "virtual_machine": "hypervisor" in flags,
        "hybrid": hybrid,
        "max_freq_khz": {str(k): v for k, v in max_freq.items()},
        "freq_classes": len(set(max_freq.values())),
        "l3_domains": list(l3.values()),
        "caches": caches,
        "scaling_driver": driver,
        "governors": (sysfs.read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors", "") or "").split(),
        "epp_choices": (sysfs.read("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_available_preferences", "") or "").split(),
    }


# ---------------------------------------------------------------- memoria
def memory() -> dict:
    mem = {}
    for line in (sysfs.read("/proc/meminfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        mem[k] = int(v.split()[0]) if v.split() else 0
    swaps = []
    for line in (sysfs.read("/proc/swaps", "") or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3:
            swaps.append({"device": parts[0], "type": parts[1], "size_kb": int(parts[2]),
                          "zram": "/zram" in parts[0]})
    return {
        "total_mb": mem.get("MemTotal", 0) // 1024,
        "numa_nodes": len(sysfs.ls("/sys/devices/system/node/node[0-9]*")) or 1,
        "swaps": swaps,
        "disk_swap": any(not s["zram"] for s in swaps),
        "zswap_enabled": sysfs.read("/sys/module/zswap/parameters/enabled") == "Y",
        "thp": sysfs.read("/sys/kernel/mm/transparent_hugepage/enabled"),
        "mglru": sysfs.read("/sys/kernel/mm/lru_gen/enabled"),
    }


# ---------------------------------------------------------------- GPU
def gpus() -> list[dict]:
    out = []
    for card in sysfs.ls("/sys/class/drm/card[0-9]*"):
        if "-" in os.path.basename(card):  # conectores (card0-HDMI-A-1)
            continue
        dev = f"{card}/device"
        vendor = sysfs.read(f"{dev}/vendor")
        driver_link = sysfs.path(f"{dev}/driver")
        driver = os.path.basename(os.readlink(driver_link)) if os.path.islink(driver_link) else None
        cur_s, cur_w = sysfs.read(f"{dev}/current_link_speed"), sysfs.read(f"{dev}/current_link_width")
        max_s, max_w = sysfs.read(f"{dev}/max_link_speed"), sysfs.read(f"{dev}/max_link_width")
        g = {
            "card": os.path.basename(card),
            "vendor": GPU_VENDORS.get(vendor or "", vendor),
            "device_id": sysfs.read(f"{dev}/device"),
            "driver": driver,
            "boot_vga": sysfs.read(f"{dev}/boot_vga") == "1",
            "pcie_current": {"speed": cur_s, "width": cur_w, "gbs": pcie_gbs(cur_s, cur_w)},
            "pcie_max": {"speed": max_s, "width": max_w, "gbs": pcie_gbs(max_s, max_w)},
            "vram_mb": (sysfs.read_int(f"{dev}/mem_info_vram_total", 0) or 0) // (1 << 20),
        }
        # Enlace degradado: tarjeta en ranura x4, riser defectuoso, BIOS en Gen1...
        # (en reposo algunas GPUs bajan la velocidad: se compara también el ancho)
        cg, mg = g["pcie_current"]["gbs"], g["pcie_max"]["gbs"]
        g["pcie_degraded"] = bool(cur_w and max_w and int(cur_w) < int(max_w))
        g["pcie_ratio"] = round(cg / mg, 2) if cg and mg else None
        # Límite de potencia de la GPU (amdgpu / nouveau / xe exponen hwmon)
        for hw in sysfs.ls(f"{dev}/hwmon/hwmon*"):
            cap = sysfs.read_int(f"{hw}/power1_cap")
            if cap:
                g["power_cap_w"] = cap / 1e6
                g["power_cap_max_w"] = (sysfs.read_int(f"{hw}/power1_cap_max") or 0) / 1e6
                g["power_cap_default_w"] = (sysfs.read_int(f"{hw}/power1_cap_default") or 0) / 1e6
                g["power_hwmon"] = hw
        g["overdrive"] = sysfs.read(f"{dev}/pp_sclk_od") is not None
        g["integrated"] = g["vendor"] in ("amd", "intel") and g["boot_vga"] and (g["vram_mb"] or 0) <= 2048
        out.append(g)
    return out


# ---------------------------------------------------------------- almacenamiento
def storage() -> list[dict]:
    out = []
    for blk in sysfs.ls("/sys/block/*"):
        name = os.path.basename(blk)
        if re.match(r"^(loop|ram|zram|dm-|md|sr|fd)", name):
            continue
        sched = sysfs.read(f"{blk}/queue/scheduler", "") or ""
        m = re.search(r"\[(\S+)\]", sched)
        out.append({
            "name": name,
            "model": sysfs.read(f"{blk}/device/model"),
            "type": "nvme" if name.startswith("nvme") else
                    "virtual" if re.match(r"^(vd|xvd)", name) else
                    "hdd" if sysfs.read(f"{blk}/queue/rotational") == "1" else
                    "mmc" if name.startswith("mmcblk") else "ssd",
            "removable": sysfs.read(f"{blk}/removable") == "1",
            "size_gb": round((sysfs.read_int(f"{blk}/size", 0) or 0) * 512 / 1e9, 1),
            "scheduler": m.group(1) if m else None,
            "schedulers": sched.replace("[", "").replace("]", "").split(),
            "nr_requests": sysfs.read_int(f"{blk}/queue/nr_requests"),
            "read_ahead_kb": sysfs.read_int(f"{blk}/queue/read_ahead_kb"),
        })
    return out


# ---------------------------------------------------------------- potencia
def power() -> dict:
    zones = []
    for z in sysfs.ls("/sys/class/powercap/intel-rapl:*"):
        if z.count(":") != 1:  # solo zonas de paquete (subzonas core/uncore aparte)
            continue
        constraints = {}
        for c in range(4):
            name = sysfs.read(f"{z}/constraint_{c}_name")
            if not name:
                break
            constraints[name] = {
                "limit_w": (sysfs.read_int(f"{z}/constraint_{c}_power_limit_uw") or 0) / 1e6,
                "max_w": (sysfs.read_int(f"{z}/constraint_{c}_max_power_uw") or 0) / 1e6,
                "window_us": sysfs.read_int(f"{z}/constraint_{c}_time_window_us"),
                "index": c,
            }
        zones.append({"zone": z, "name": sysfs.read(f"{z}/name"), "constraints": constraints,
                      "enabled": sysfs.read(f"{z}/enabled") == "1"})
    batteries = [p for p in sysfs.ls("/sys/class/power_supply/*")
                 if sysfs.read(f"{p}/type") == "Battery"]
    return {
        "rapl": zones,
        "platform_profile": sysfs.read("/sys/firmware/acpi/platform_profile"),
        "platform_profiles": (sysfs.read("/sys/firmware/acpi/platform_profile_choices", "") or "").split(),
        "battery": bool(batteries),
        "on_ac": any(sysfs.read(f"{p}/online") == "1" for p in sysfs.ls("/sys/class/power_supply/*")
                     if sysfs.read(f"{p}/type") == "Mains") or not batteries,
        "laptop": sysfs.read("/sys/class/dmi/id/chassis_type") in LAPTOP_CHASSIS,
    }


# ---------------------------------------------------------------- arranque
def boot_drivers() -> dict:
    """Drivers necesarios para montar / : se integran en el kernel para arrancar
    sin cargar módulos (más rápido y sin depender del initramfs)."""
    root_dev, fstype = None, None
    for line in (sysfs.read("/proc/mounts", "") or "").splitlines():
        parts = line.split()
        if len(parts) > 2 and parts[1] == "/":
            root_dev, fstype = parts[0], parts[2]
    drivers: list[str] = []
    if root_dev and root_dev.startswith("/dev/"):
        _disk_drivers(os.path.basename(os.path.realpath(root_dev)), drivers, depth=0)
    return {"root_device": root_dev, "root_fs": fstype, "drivers": drivers}


def _disk_drivers(name: str, drivers: list[str], depth: int) -> None:
    """Drivers de un dispositivo de bloque. Sigue dm-crypt/LVM/RAID (dm-*, md*) hasta los discos reales."""
    if depth > 4:
        return
    slaves = [os.path.basename(p) for p in sysfs.ls(f"/sys/block/{name}/slaves/*")]
    if slaves:
        uuid = sysfs.read(f"/sys/block/{name}/dm/uuid") or ""
        for mod in (["dm_mod", "dm_crypt"] if uuid.startswith("CRYPT-") else ["dm_mod"] if name.startswith("dm-")
                    else ["md_mod"] if name.startswith("md") else []):
            if mod not in drivers:
                drivers.append(mod)
        for sl in slaves:
            _disk_drivers(sl, drivers, depth + 1)
        return
    # partición -> disco padre
    disk = next((os.path.basename(os.path.dirname(p)) for p in sysfs.ls(f"/sys/block/*/{name}")), name)
    p = os.path.realpath(sysfs.path(f"/sys/block/{disk}/device"))
    root = sysfs.path("/")
    while p.startswith(os.path.join(root, "sys/devices")) and p != root:
        link = os.path.join(p, "driver")
        if os.path.islink(link):
            d = os.path.basename(os.readlink(link))
            if d not in drivers:
                drivers.append(d)
        p = os.path.dirname(p)


def memory_modules() -> list[dict]:
    """Módulos de RAM instalados (tabla SMBIOS 17, legible por root en /sys/firmware/dmi/entries)."""
    import struct
    mods = []
    for e in sysfs.ls("/sys/firmware/dmi/entries/17-*"):
        try:
            raw = open(sysfs.path(f"{e}/raw"), "rb").read()
        except OSError:
            continue
        if len(raw) < 0x17:
            continue
        size = struct.unpack_from("<H", raw, 0x0C)[0]
        if size in (0, 0xFFFF):
            continue                                   # zócalo vacío
        # Bit 15 = 0: el valor está en MB; bit 15 = 1: en KB (SMBIOS 3.x, tipo 17, desplazamiento 0Ch)
        mb = (size & 0x7FFF) // 1024 if size & 0x8000 else size & 0x7FFF
        if size == 0x7FFF and len(raw) >= 0x20:
            mb = struct.unpack_from("<I", raw, 0x1C)[0]
        speed = struct.unpack_from("<H", raw, 0x15)[0]
        conf = struct.unpack_from("<H", raw, 0x20)[0] if len(raw) >= 0x22 else 0
        mods.append({"mb": mb, "speed_mts": speed, "configured_mts": conf or speed})
    return mods


def product() -> str:
    return " ".join(filter(None, (sysfs.read("/sys/class/dmi/id/sys_vendor"),
                                  sysfs.read("/sys/class/dmi/id/product_version"),
                                  sysfs.read("/sys/class/dmi/id/product_name"))))


def cpu_vulnerabilities() -> dict[str, str]:
    """Estado de mitigación de cada vulnerabilidad de CPU según el kernel."""
    out = {}
    for f in sysfs.ls("/sys/devices/system/cpu/vulnerabilities/*"):
        out[os.path.basename(f)] = sysfs.read(f, "") or ""
    return out


def loaded_modules() -> list[str]:
    return sorted(line.split()[0] for line in (sysfs.read("/proc/modules", "") or "").splitlines() if line)


def probe() -> dict:
    return {
        "cpu": cpu(),
        "memory": memory(),
        "gpus": gpus(),
        "storage": storage(),
        "power": power(),
        "boot": boot_drivers(),
        "modules": loaded_modules(),
        "cpu_vulnerabilities": cpu_vulnerabilities(),
        "memory_modules": memory_modules(),
        "product": product(),
        "boost": sysfs.read("/sys/devices/system/cpu/cpufreq/boost"),
        "kernel": sysfs.read("/proc/sys/kernel/osrelease"),
    }


def summarize(p: dict) -> list[str]:
    c, m = p["cpu"], p["memory"]
    lines = [f"CPU: {c['model']} — {c['physical_cores']} núcleos / {c['logical_cpus']} hilos"
             f"{' (híbrida P/E)' if c['hybrid'] else ''}, {len(c['l3_domains'])} dominio(s) L3"]
    lines.append(f"RAM: {m['total_mb']} MB, {m['numa_nodes']} nodo(s) NUMA")
    for g in p["gpus"]:
        cur, mx = g["pcie_current"], g["pcie_max"]
        warn = "  ⚠ ENLACE DEGRADADO" if g["pcie_degraded"] else ""
        lines.append(f"GPU {g['card']}: {g['vendor']} ({g['driver']}) PCIe {cur['speed']} x{cur['width']}"
                     f" de {mx['speed']} x{mx['width']}{warn}")
    for d in p["storage"]:
        lines.append(f"Disco {d['name']}: {d['type']} {d['size_gb']} GB, planificador {d['scheduler']}")
    vul = [k for k, v in p.get("cpu_vulnerabilities", {}).items() if v.startswith("Vulnerable")]
    lines.append("Vulnerabilidades de CPU: " + (", ".join(vul) + " SIN MITIGAR" if vul else "todas mitigadas o no afectan"))
    pw = p["power"]
    for z in pw["rapl"]:
        lim = ", ".join(f"{k} {v['limit_w']:.0f} W" for k, v in z["constraints"].items())
        lines.append(f"Potencia {z['name']}: {lim}")
    if pw["platform_profiles"]:
        lines.append(f"Perfiles de plataforma: {' '.join(pw['platform_profiles'])} (actual: {pw['platform_profile']})")
    return lines


