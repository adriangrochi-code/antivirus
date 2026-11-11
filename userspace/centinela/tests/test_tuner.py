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
