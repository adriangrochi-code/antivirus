import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from centinela.apply import Actuator  # noqa: E402
from centinela.tuner import make_plan  # noqa: E402

HW = {
    "cpu": {"vendor": "AuthenticAMD", "model": "Ryzen 9", "logical_cpus": 32, "physical_cores": 16,
            "flags": ["avx2", "avx512f"], "virtual_machine": False, "hybrid": {}, "freq_classes": 2,
            "l3_domains": [[0, 1], [2, 3]], "governors": ["performance", "powersave"],
            "epp_choices": ["performance", "balance_performance", "power"]},
    "memory": {"total_mb": 32768, "numa_nodes": 1, "disk_swap": False},
    "gpus": [{"card": "card0", "pcie_degraded": True, "pcie_current": {"width": "4"}, "pcie_max": {"width": "16"},
              "power_cap_w": 200, "power_cap_max_w": 250, "power_cap_default_w": 200,
              "power_hwmon": "/sys/class/drm/card0/device/hwmon/hwmon3"}],
    "storage": [{"name": "nvme0n1", "type": "nvme", "schedulers": ["none", "mq-deadline"]},
                {"name": "sda", "type": "hdd", "schedulers": ["mq-deadline", "bfq", "none"]}],
    "power": {"rapl": [{"zone": "/sys/class/powercap/intel-rapl:0", "name": "package-0", "constraints": {
        "long_term": {"limit_w": 120, "max_w": 170, "index": 0}}}], "platform_profiles": ["low-power", "balanced", "performance"],
        "battery": False},
    "boot": {"root_device": "/dev/nvme0n1p2", "root_fs": "ext4", "drivers": ["nvme"]},
    "cpu_vulnerabilities": {"spectre_v2": "Mitigation: Retpolines", "gather_data_sampling": "Vulnerable: No microcode"},
}


def test_gaming_performance_plan():
    p = make_plan(HW, {}, "juegos", "rendimiento")
    assert "CONFIG_X86_NATIVE_CPU=y" in p.kernel_fragment
    assert "# CONFIG_CPU_SUP_INTEL is not set" in p.kernel_fragment
    assert "CONFIG_SCHED_MC_PRIO=y" in p.kernel_fragment            # CCDs + preferred cores
    assert p.builtin_modules == ["ext4", "nvme"]
    assert p.io_schedulers["nvme0n1"]["scheduler"] == "none" and p.io_schedulers["sda"]["scheduler"] == "bfq"
    assert p.zram and p.sysctl["vm.swappiness"] == "180"
    assert p.power["rapl"]["/sys/class/powercap/intel-rapl:0:0"]["target_w"] == 170
    assert p.power["platform_profile"] == "performance"
    assert p.sched_ext == "scx_lavd"
    assert "mitigations=auto" in p.cmdline and "mitigations=off" not in p.cmdline
    assert any("PCIe x4" in r for r in p.recommendations)
    assert any("gather_data_sampling" in r for r in p.recommendations)


def test_quiet_profile_lowers_limits():
    p = make_plan(HW, {}, "escritorio", "silencioso")
    assert p.power["rapl"]["/sys/class/powercap/intel-rapl:0:0"]["target_w"] == 72
    assert p.cpu["epp"] == "power"


def test_actuator_whitelist():
    a = Actuator(dry_run=True)
    assert a.allowed("/proc/sys/vm/swappiness")
    assert not a.allowed("/proc/sys/kernel/modules_disabled")
    assert not a.allowed("/sys/block/../../etc/shadow")
    assert not a.allowed("/proc/sys/vm/mmap_min_addr")              # antes lo permitía «vm/*»
    assert not a.allowed("/proc/sys/vm/unprivileged_userfaultfd")
    assert not a.allowed("/sys/class/drm/card0/device/hwmon/hwmon1/x/power1_cap")  # «*» no cruza «/»
    assert a.allowed("/sys/class/drm/card0/device/hwmon/hwmon1/power1_cap")


def test_initramfs_compressor_follows_disk_speed():
    fast = make_plan(HW, {"storage": {"nvme0n1": {"seq_read_mbs": 3200, "rand4k_read_us": 60}}})
    slow = make_plan(dict(HW, boot={"root_device": "/dev/sda1", "root_fs": "ext4", "drivers": []}),
                     {"storage": {"sda": {"seq_read_mbs": 140, "rand4k_read_us": 9000}}})
    assert fast.initramfs_compress == "lz4" and slow.initramfs_compress == "zstd"


def test_boot_analysis_parsing():
    from centinela import boot
    t = boot.parse_time("Startup finished in 7.112s (firmware) + 2.010s (loader) + 1.402s (kernel) + "
                        "3.215s (initrd) + 9.876s (userspace) = 23.615s\ngraphical.target reached after 9.811s in userspace.")
    assert t["firmware"] == 7.112 and t["escritorio"] == 9.811
    b = boot.parse_blame("  4.512s plymouth-quit-wait.service\n 2.1s ModemManager.service\n  812ms nftables.service\n"
                         "1min 2.300s apt-daily.service")
    assert b[0] == (4.512, "plymouth-quit-wait.service") and b[-1][0] == 62.3
    rec = boot.recommendations(t, b)
    assert any("BIOS" in r for r in rec) and any("ModemManager" in r for r in rec)
    assert not any("nftables" in r for r in rec)          # esencial y rápido: no se toca
