"""Convierte el perfil de hardware + mediciones en un plan de ajuste a medida.

Función pura: no toca el sistema (eso lo hace ``apply``), por lo que es fácil
de probar y de revisar antes de aplicar (``centinela plan``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

USE_CASES = ("escritorio", "juegos", "servidor", "portatil")
POWER_PROFILES = ("silencioso", "equilibrado", "rendimiento")

# Parámetros de arranque fijos: seguridad (nunca se desactivan mitigaciones).
SECURITY_CMDLINE = [
    "lsm=landlock,lockdown,yama,integrity,apparmor,bpf",
    "lockdown=integrity", "module.sig_enforce=1",
    "init_on_alloc=1", "init_on_free=1", "page_alloc.shuffle=1",
    "randomize_kstack_offset=on", "slab_nomerge", "vsyscall=none",
    "debugfs=off", "iommu.strict=1", "iommu.passthrough=0",
    "efi=disable_early_pci_dma", "mitigations=auto",
]


@dataclass
class Plan:
    use_case: str
    power_profile: str
    kernel_fragment: list[str] = field(default_factory=list)
    builtin_modules: list[str] = field(default_factory=list)
    cmdline: list[str] = field(default_factory=list)
    sysctl: dict[str, str] = field(default_factory=dict)
    io_schedulers: dict[str, dict] = field(default_factory=dict)
    zram: dict = field(default_factory=dict)
    zswap: dict = field(default_factory=dict)
    cpu: dict = field(default_factory=dict)
    power: dict = field(default_factory=dict)
    sched_ext: str | None = None
    initramfs_compress: str = "zstd"
    recommendations: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def why(self, msg: str) -> None:
        self.reasons.append(msg)


def _round_pow2(n: int, minimum: int = 8) -> int:
    p = minimum
    while p < n:
        p *= 2
    return p


# ---------------------------------------------------------------- kernel
def _kernel(plan: Plan, hw: dict) -> None:
    c, m = hw["cpu"], hw["memory"]
    k = plan.kernel_fragment
    k.append("# Generado por `centinela plan` para este equipo concreto")

    if c.get("virtual_machine"):
        plan.why("Máquina virtual: no se usa -march=native (la VM podría migrar de CPU).")
    else:
        k += ["CONFIG_X86_NATIVE_CPU=y"]
        plan.why(f"Kernel compilado con -march=native para {c.get('model')} "
                 f"(usa {', '.join(c.get('flags', [])) or 'sus extensiones'}).")

    nr = _round_pow2(c.get("logical_cpus", 8))
    k += ["# CONFIG_MAXSMP is not set", f"CONFIG_NR_CPUS={nr}"]
    plan.why(f"NR_CPUS={nr}: menos memoria por-CPU reservada que el kernel genérico (8192).")

    vendor = c.get("vendor") or ""
    if vendor in ("GenuineIntel", "AuthenticAMD"):
        k += ["CONFIG_PROCESSOR_SELECT=y", "# CONFIG_CPU_SUP_HYGON is not set",
              "# CONFIG_CPU_SUP_CENTAUR is not set", "# CONFIG_CPU_SUP_ZHAOXIN is not set"]
        if vendor == "GenuineIntel":
            k += ["CONFIG_CPU_SUP_INTEL=y", "# CONFIG_CPU_SUP_AMD is not set",
                  "CONFIG_X86_INTEL_PSTATE=y", "CONFIG_INTEL_HFI_THERMAL=y"]
            plan.cmdline.append("intel_pstate=active")
        else:
            k += ["CONFIG_CPU_SUP_AMD=y", "# CONFIG_CPU_SUP_INTEL is not set",
                  "CONFIG_X86_AMD_PSTATE=y", "CONFIG_X86_AMD_PSTATE_DEFAULT_MODE=3"]
            plan.cmdline.append("amd_pstate=active")
        plan.why(f"Solo soporte para CPUs {vendor}: kernel más pequeño, menos código en arranque.")

    if c.get("hybrid") or c.get("freq_classes", 1) > 1 or len(c.get("l3_domains", [])) > 1:
        k += ["CONFIG_SCHED_MC=y", "CONFIG_SCHED_MC_PRIO=y", "CONFIG_SCHED_CLUSTER=y"]
        plan.why("CPU asimétrica (núcleos P/E, preferred cores o varios CCD): el planificador "
                 "conoce la topología y coloca los hilos críticos en los núcleos rápidos.")

    if m.get("numa_nodes", 1) <= 1:
        k.append("# CONFIG_NUMA_BALANCING is not set")
        plan.why("Un solo nodo NUMA: se desactiva el balanceo NUMA (evita fallos de página inútiles).")

    if plan.use_case == "servidor":
        k += ["CONFIG_PREEMPT_VOLUNTARY=y", "CONFIG_HZ_250=y"]
        plan.cmdline.append("preempt=voluntary")
        plan.why("Servidor: preempción voluntaria y HZ=250 maximizan el rendimiento total.")
    else:
        k += ["CONFIG_PREEMPT_DYNAMIC=y", "CONFIG_HZ_1000=y"]
        plan.cmdline.append("preempt=full")
        plan.why(f"{plan.use_case.capitalize()}: preempción completa y HZ=1000 para mínima latencia de respuesta.")

    # Drivers de arranque integrados: sin cargar módulos para montar la raíz.
    boot = hw.get("boot", {})
    builtin = [d.replace("-", "_") for d in boot.get("drivers", [])]
    if boot.get("root_fs"):
        builtin.append(boot["root_fs"])
    plan.builtin_modules = sorted(set(builtin))
    if builtin:
        plan.why(f"Integrados en el kernel ({', '.join(plan.builtin_modules)}): la raíz se monta "
                 "sin esperar a cargar módulos → arranque más rápido.")


# ---------------------------------------------------------------- memoria
def _memory(plan: Plan, hw: dict, bench: dict) -> None:
    m, c = hw["memory"], hw["cpu"]
    ram = m.get("total_mb", 0)
    cores = c.get("physical_cores") or c.get("logical_cpus") or 1

    comp = bench.get("compression", {})
    algo = None
    if comp.get("zstd") and comp.get("lz4"):
        # Coste de recuperar una página de 4 KiB comprimida vs. beneficio de mejor ratio.
        z, l4 = comp["zstd"], comp["lz4"]
        extra_us = 4096 / (z["decompress_mbs"] * 1e6) * 1e6 - 4096 / (l4["decompress_mbs"] * 1e6) * 1e6
        gain = z["ratio"] / l4["ratio"] - 1
        algo = "zstd" if (ram <= 16384 and gain > 0.10) or extra_us < 1.0 else "lz4"
        plan.why(f"Compresión medida: zstd ratio {z['ratio']:.2f} / lz4 {l4['ratio']:.2f}, "
                 f"+{extra_us:.2f} µs por página con zstd → {algo}.")
    if not algo:
        algo = "zstd" if ram <= 16384 or cores >= 8 else "lz4"
        plan.why(f"Sin mediciones de compresión: {algo} por RAM ({ram} MB) y {cores} núcleos.")

    if m.get("disk_swap"):
        plan.zswap = {"enabled": True, "compressor": algo, "max_pool_percent": 25,
                      "shrinker_enabled": True}
        plan.cmdline += ["zswap.enabled=1", f"zswap.compressor={algo}"]
        plan.sysctl["vm.swappiness"] = "100"
        plan.why("Hay swap en disco: zswap comprime en RAM antes de escribir al disco.")
    else:
        size = "ram" if ram <= 4096 else "ram / 2" if ram <= 32768 else "min(ram / 4, 16384)"
        plan.zram = {"zram-size": size, "compression-algorithm": algo, "swap-priority": 100}
        plan.cmdline.append("zswap.enabled=0")
        plan.sysctl.update({"vm.swappiness": "180", "vm.page-cluster": "0",
                            "vm.watermark_boost_factor": "0", "vm.watermark_scale_factor": "125"})
        plan.why(f"Sin swap en disco: zram ({size}) con {algo}; swappiness alta porque "
                 "el swap comprimido en RAM es mucho más rápido que releer desde disco.")

    plan.sysctl["vm.vfs_cache_pressure"] = "50" if ram >= 16384 else "100"
    plan.cmdline.append("transparent_hugepage=madvise")
    if ram >= 16384 and plan.use_case in ("juegos", "servidor"):
        plan.sysctl["vm.max_map_count"] = "2147483642"  # juegos con Proton lo necesitan
    if plan.use_case == "juegos":
        plan.sysctl["vm.max_map_count"] = "2147483642"


# ---------------------------------------------------------------- almacenamiento
def _storage(plan: Plan, hw: dict, bench: dict) -> None:
    lat = bench.get("storage", {})
    for d in hw.get("storage", []):
        avail = d.get("schedulers") or []
        t = d["type"]
        measured = lat.get(d["name"], {}).get("rand4k_read_us")
        if t == "hdd" and measured is not None and measured < 1000:
            t = "ssd"  # el hardware dice rotacional pero la latencia medida es de SSD
        if t in ("nvme", "virtual"):
            choice, ra = "none", 128
        elif t == "hdd":
            choice, ra = "bfq", 2048
        elif t == "mmc" or d.get("removable"):
            choice, ra = "bfq", 512
        else:
            choice, ra = "mq-deadline", 256
            # SSD muy rápido (latencia medida baja): sin planificador
            if lat.get(d["name"], {}).get("rand4k_read_us", 1e9) < 100:
                choice = "none"
        if avail and choice not in avail:
            choice = next((s for s in ("mq-deadline", "none") if s in avail), d.get("scheduler"))
        plan.io_schedulers[d["name"]] = {"type": t, "scheduler": choice, "read_ahead_kb": ra}
    # Compresor del initramfs: en discos muy rápidos manda la descompresión (lz4); en lentos, el tamaño (zstd)
    root = _root_disk(hw)
    seq = lat.get(root, {}).get("seq_read_mbs") if root else None
    if seq and seq > 1500:
        plan.initramfs_compress = "lz4"
        plan.why(f"Initramfs con lz4: el disco del sistema lee a {seq:.0f} MB/s, así que descomprimir rápido "
                 "importa más que el tamaño.")
    elif seq:
        plan.why(f"Initramfs con zstd: el disco lee a {seq:.0f} MB/s, leer menos bytes compensa.")
    if plan.io_schedulers:
        plan.why("Planificador de E/S por disco: NVMe y discos virtuales sin planificador, "
                 "SSD mq-deadline, HDD/USB con BFQ (prioriza la interactividad).")


# ---------------------------------------------------------------- CPU y potencia
EPP = {"silencioso": "power", "equilibrado": "balance_performance", "rendimiento": "performance"}
PLATFORM = {"silencioso": ["quiet", "low-power"], "equilibrado": ["balanced"],
            "rendimiento": ["performance", "balanced-performance"]}


def _cpu_power(plan: Plan, hw: dict) -> None:
    c, pw = hw["cpu"], hw["power"]
    prof = plan.power_profile
    gov = c.get("governors") or []
    if c.get("epp_choices"):
        plan.cpu = {"governor": "powersave" if "powersave" in gov else None, "epp": EPP[prof]}
        if prof == "rendimiento" and "performance" in gov and not pw.get("battery"):
            plan.cpu = {"governor": "performance", "epp": "performance"}
    else:
        plan.cpu = {"governor": "performance" if prof == "rendimiento" and "performance" in gov
                    else "schedutil" if "schedutil" in gov else (gov[0] if gov else None), "epp": None}

    choices = pw.get("platform_profiles") or []
    plat = next((p for p in PLATFORM[prof] if p in choices), None)

    rapl = {}
    for z in pw.get("rapl", []):
        if z.get("name", "").startswith("package"):
            for cname, cons in z["constraints"].items():
                cur, mx = cons["limit_w"], cons["max_w"]
                if not cur:
                    continue
                if prof == "rendimiento" and mx and mx > cur:
                    target = mx
                elif prof == "silencioso":
                    target = round(cur * 0.6, 1)
                else:
                    target = cur
                rapl[f"{z['zone']}:{cons['index']}"] = {"name": cname, "default_w": cur, "target_w": target}

    gpu_caps = {}
    for g in hw.get("gpus", []):
        if g.get("power_cap_w"):
            d, mx = g.get("power_cap_default_w") or g["power_cap_w"], g.get("power_cap_max_w") or g["power_cap_w"]
            target = mx if prof == "rendimiento" else round(d * 0.7, 1) if prof == "silencioso" else d
            gpu_caps[g["power_hwmon"]] = {"default_w": d, "target_w": target}

    plan.power = {"platform_profile": plat, "rapl": rapl, "gpu_power_caps": gpu_caps}
    plan.why(f"Perfil de potencia '{prof}': EPP={plan.cpu.get('epp')}, perfil de plataforma={plat}, "
             f"{len(rapl)} límite(s) RAPL y {len(gpu_caps)} GPU ajustables.")

    if plan.use_case in ("juegos", "escritorio"):
        plan.sysctl["kernel.split_lock_mitigate"] = "0"
        plan.why("split_lock_mitigate=0: evita congelones de algunos juegos de Windows bajo Proton.")
    plan.cmdline += ["nowatchdog", "nmi_watchdog=0"]


# ---------------------------------------------------------------- planificador
def _sched_ext(plan: Plan, hw: dict) -> None:
    c = hw["cpu"]
    asym = bool(c.get("hybrid")) or len(c.get("l3_domains", [])) > 1 or c.get("freq_classes", 1) > 1
    if plan.use_case in ("juegos", "escritorio") or (plan.use_case == "portatil" and asym):
        plan.sched_ext = "scx_lavd"
        plan.why("sched_ext scx_lavd: planificador consciente de latencia que prioriza "
                 "las tareas interactivas (juegos, audio, escritorio).")
    elif plan.use_case == "servidor" and asym:
        plan.sched_ext = "scx_bpfland"


# ---------------------------------------------------------------- avisos
def _recommendations(plan: Plan, hw: dict, bench: dict) -> None:
    r = plan.recommendations
    for g in hw.get("gpus", []):
        if g.get("pcie_degraded"):
            r.append(f"La GPU {g['card']} funciona a PCIe x{g['pcie_current']['width']} de "
                     f"x{g['pcie_max']['width']} posibles: revisa la ranura, el riser o la BIOS.")
    for d in hw.get("storage", []):
        if d["type"] == "hdd" and not d.get("removable") and d["name"] == _root_disk(hw):
            r.append(f"El sistema está en un disco mecánico ({d['name']}): un SSD reduce el arranque varias veces.")
    mb = bench.get("memory", {}).get("bandwidth_gbs", {})
    if mb and hw["cpu"].get("logical_cpus", 1) >= 8 and mb.get("read", 99) < 20:
        r.append(f"Ancho de banda de RAM bajo ({mb['read']:.1f} GB/s): ¿módulos en un solo canal "
                 "o perfil XMP/EXPO desactivado?")
    for name, state in hw.get("cpu_vulnerabilities", {}).items():
        if state.startswith("Vulnerable"):
            hint = "actualiza el microcódigo (paquetes intel-microcode / amd64-microcode) y la BIOS"
            if "SMT" in state or name in ("mds", "l1tf", "mmio_stale_data"):
                hint += "; considera desactivar SMT (nosmt) si el equipo ejecuta código no confiable"
            r.append(f"CPU vulnerable a {name} ({state}): {hint}.")
    if hw["memory"].get("total_mb", 0) < 4096:
        r.append("Menos de 4 GB de RAM: zram es imprescindible; evita Waydroid y juegos pesados.")
    if hw["power"].get("battery") and plan.power_profile == "rendimiento":
        r.append("Portátil en perfil rendimiento: más temperatura y menos batería.")


def _root_disk(hw: dict) -> str | None:
    dev = (hw.get("boot") or {}).get("root_device") or ""
    for d in hw.get("storage", []):
        if dev.startswith(f"/dev/{d['name']}"):
            return d["name"]
    return None


def make_plan(hw: dict, bench: dict | None = None, use_case: str = "escritorio",
              power_profile: str = "equilibrado") -> Plan:
    if use_case not in USE_CASES:
        raise ValueError(f"Uso desconocido: {use_case} (opciones: {', '.join(USE_CASES)})")
    if power_profile not in POWER_PROFILES:
        raise ValueError(f"Perfil desconocido: {power_profile} (opciones: {', '.join(POWER_PROFILES)})")
    bench = bench or {}
    plan = Plan(use_case=use_case, power_profile=power_profile, cmdline=list(SECURITY_CMDLINE))
    _kernel(plan, hw)
    _memory(plan, hw, bench)
    _storage(plan, hw, bench)
    _cpu_power(plan, hw)
    _sched_ext(plan, hw)
    _recommendations(plan, hw, bench)
    plan.cmdline = list(dict.fromkeys(plan.cmdline))
    return plan
