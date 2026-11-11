import os
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


A275_HW = {**HW, "product": "LENOVO ThinkPad A275 20KD0013US",
           "cpu": {**HW["cpu"], "logical_cpus": 4, "physical_cores": 2, "l3_domains": [], "freq_classes": 1,
                   "epp_choices": [], "governors": ["performance", "schedutil"], "vendor": "AuthenticAMD"},
           "memory": {"total_mb": 15800, "numa_nodes": 1, "disk_swap": False},
           "memory_modules": [{"mb": 16384, "speed_mts": 2400, "configured_mts": 1866}],
           "gpus": [{"card": "card0", "vendor": "amd", "integrated": True, "overdrive": True, "boot_vga": True,
                     "vram_mb": 512, "pcie_degraded": False}],
           "power": {"rapl": [], "platform_profiles": [], "battery": True}}


def test_a275_shared_memory_profile():
    p = make_plan(A275_HW, {"memory": {"bandwidth_gbs": {"read": 11.8}}}, "escritorio", "rendimiento")
    assert p.desktop_light
    assert any("canal simple" in r for r in p.recommendations)
    assert any("0x09" in r and "AmdSetup" in r for r in p.recommendations)
    assert p.gpu_oc_percent == 10 and "amdgpu.ppfeaturemask=0xffffffff" in p.cmdline
    assert "amdgpu.gttsize=4096" in p.cmdline
    quiet = make_plan(A275_HW, {"memory": {"bandwidth_gbs": {"read": 11.8}}}, "escritorio", "equilibrado")
    assert quiet.gpu_oc_percent == 0                 # el OC solo en perfil rendimiento


def test_light_kwinrc_keeps_other_settings():
    from centinela.apply import light_kwinrc
    out = light_kwinrc("[Plugins]\nblurEnabled=true\nsquashEnabled=true\n\n[Windows]\nElectricBorderTiling=true\n")
    assert "blurEnabled=false" in out and "squashEnabled=true" in out and "ElectricBorderTiling=true" in out
    assert "translucencyEnabled=false" in out


def test_oc_clamped_and_whitelisted():
    from centinela.apply import Actuator, OC_MAX
    assert Actuator.allowed("/sys/class/drm/card0/device/pp_sclk_od")
    assert OC_MAX <= 15


def test_a275_memory_reclaim_oomd_and_threads(tmp_path, monkeypatch):
    hw = {**A275_HW, "cpu": {**A275_HW["cpu"], "cmt": True, "execution_units": [[0, 1], [2, 3]]}}
    p = make_plan(hw, {}, "escritorio", "equilibrado")
    assert p.damon["enabled"] == "Y" and p.damon["min_age"] == "120000000"
    assert "damon_reclaim.enabled=Y" in p.cmdline and f"damon_reclaim.quota_sz={64 << 20}" in p.cmdline
    assert p.oomd["DefaultMemoryPressureDurationSec"] == "20s"
    assert p.threads["ia_generacion"] == 2 and p.threads["ia_lote"] == 4
    assert any("CMT" in r for r in p.reasons)
    big = make_plan(HW, {}, "juegos", "rendimiento")         # 32 GB y GPU dedicada: sin DAMON
    assert not big.damon and big.oomd and big.threads["ia_generacion"] == 16

    from centinela import apply, sysfs
    from centinela.apply import render_files
    files = render_files(p)
    assert "DefaultMemoryPressureLimit=60%" in files["systemd/oomd.conf.d/60-centinela.conf"]
    assert '"ia_generacion": 2' in files["centinela/hilos.json"]

    # DAMON en marcha: se escriben los parámetros y se confirman con commit_inputs; parado: se activa.
    base = tmp_path / "sys/module/damon_reclaim/parameters"
    base.mkdir(parents=True)
    for k, v in {"enabled": "Y", "min_age": "120000000", "quota_ms": "10", "quota_sz": "134217728",
                 "quota_reset_interval_ms": "1000", "commit_inputs": "N"}.items():
        (base / k).write_text(v)
    monkeypatch.setattr(sysfs, "ROOT", str(tmp_path)) if hasattr(sysfs, "ROOT") else None
    monkeypatch.setattr(sysfs, "path", lambda p: str(tmp_path) + p)
    monkeypatch.setattr(sysfs, "read", lambda p, d=None: (open(str(tmp_path) + p).read().strip()
                                                         if Path(str(tmp_path) + p).exists() else d))
    act = Actuator()
    apply.apply_damon(p, act)
    assert (base / "quota_sz").read_text() == str(64 << 20) and (base / "commit_inputs").read_text() == "Y"
    (base / "enabled").write_text("N")
    (base / "commit_inputs").write_text("N")
    act = Actuator()
    apply.apply_damon(p, act)
    assert (base / "enabled").read_text() == "Y" and (base / "commit_inputs").read_text() == "N"


def test_boot_drivers_follow_luks_to_disk(tmp_path, monkeypatch):
    from centinela import hw, sysfs
    r = tmp_path
    (r / "proc").mkdir()
    (r / "proc/mounts").write_text("/dev/dm-0 / btrfs rw,noatime,subvol=/@ 0 0\n")
    (r / "sys/block/dm-0/slaves/nvme0n1p2").mkdir(parents=True)
    (r / "sys/block/dm-0/dm").mkdir()
    (r / "sys/block/dm-0/dm/uuid").write_text("CRYPT-LUKS2-abc-luks-abc\n")
    (r / "sys/block/nvme0n1/nvme0n1p2").mkdir(parents=True)
    dev = r / "sys/devices/pci0000:00/0000:00:01.0/nvme/nvme0"
    dev.mkdir(parents=True)
    (r / "sys/bus/pci/drivers/nvme").mkdir(parents=True)
    os.symlink("../../../bus/pci/drivers/nvme", r / "sys/devices/pci0000:00/0000:00:01.0/driver")
    os.symlink("../../devices/pci0000:00/0000:00:01.0/nvme/nvme0", r / "sys/block/nvme0n1/device")
    monkeypatch.setattr(sysfs, "ROOT", str(r))
    b = hw.boot_drivers()
    assert b["root_fs"] == "btrfs"
    assert b["drivers"][:2] == ["dm_mod", "dm_crypt"] and "nvme" in b["drivers"]


def test_zram_two_tier_compression():
    p = make_plan(A275_HW, {"compression": {"zstd": {"ratio": 3.1, "decompress_mbs": 900},
                                            "lz4": {"ratio": 2.2, "decompress_mbs": 2500}}}, "escritorio", "equilibrado")
    assert p.zram["compression-algorithm"] == "lz4 zstd"
    from centinela.apply import render_files
    assert "compression-algorithm = lz4 zstd" in render_files(p)["systemd/zram-generator.conf"]


def test_user_oc_choice_beats_plan_and_apply_keeps_prefs(tmp_path, monkeypatch):
    import json
    from centinela import apply, sysfs
    monkeypatch.setattr(apply, "STATE_DIR", str(tmp_path))
    (tmp_path / "prefs.json").write_text(json.dumps({"gpu_oc": 0, "autotune": None, "perfil": "rendimiento"}))
    root = tmp_path / "sys"
    card = root / "sys/class/drm/card0/device"
    card.mkdir(parents=True)
    (card / "pp_sclk_od").write_text("0")
    monkeypatch.setattr(sysfs, "ROOT", str(root))
    p = make_plan(A275_HW, {"memory": {"bandwidth_gbs": {"read": 11.8}}}, "escritorio", "rendimiento")
    assert p.gpu_oc_percent == 10
    act = Actuator(dry_run=True)
    apply.apply_power(p, act)
    assert not any(path.endswith("pp_sclk_od") and v == "10" for path, v in act.changes)   # el usuario puso 0


def test_energy_modes_battery_and_ac(tmp_path, monkeypatch):
    from centinela import apply, energia, sysfs
    r = tmp_path
    def mk(path, value):
        p = r / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)
    mk("/sys/class/power_supply/AC/type", "Mains")
    mk("/sys/class/power_supply/AC/online", "0")
    mk("/sys/class/power_supply/BAT0/type", "Battery")
    mk("/sys/class/power_supply/BAT0/status", "Discharging")
    mk("/sys/class/power_supply/BAT0/charge_control_end_threshold", "100")
    mk("/sys/class/power_supply/BAT0/charge_control_start_threshold", "0")
    mk("/sys/class/scsi_host/host0/link_power_management_policy", "max_performance")
    mk("/sys/module/pcie_aspm/parameters/policy", "default")
    mk("/sys/bus/pci/devices/0000:00:01.0/power/control", "on")
    mk("/sys/bus/usb/devices/1-1/idVendor", "046d")                     # ratón: no se suspende
    mk("/sys/bus/usb/devices/1-1/power/control", "on")
    mk("/sys/bus/usb/devices/1-1/1-1:1.0/bInterfaceClass", "03")
    mk("/sys/bus/usb/devices/1-2/idVendor", "0bda")                     # lector de tarjetas
    mk("/sys/bus/usb/devices/1-2/power/control", "on")
    mk("/sys/bus/usb/devices/1-2/1-2:1.0/bInterfaceClass", "08")
    mk("/proc/sys/vm/dirty_writeback_centisecs", "500")
    mk("/proc/sys/vm/stat_interval", "1")
    monkeypatch.setattr(sysfs, "ROOT", str(r))
    monkeypatch.setattr(apply, "STATE_DIR", str(tmp_path / "estado"))
    assert not energia.on_ac()
    act = Actuator(dry_run=True)
    energia.apply("bateria", act)
    ch = dict(act.changes)
    assert ch["/sys/class/scsi_host/host0/link_power_management_policy"] == "med_power_with_dipm"
    assert ch["/sys/module/pcie_aspm/parameters/policy"] == "powersupersave"
    assert ch["/sys/bus/usb/devices/1-2/power/control"] == "auto"
    assert "/sys/bus/usb/devices/1-1/power/control" not in ch               # HID intacto
    assert ch["/proc/sys/vm/dirty_writeback_centisecs"] == "1500"
    act = Actuator(dry_run=True)
    assert "80 %" in energia.set_threshold(80, act)
    assert dict(act.changes)["/sys/class/power_supply/BAT0/charge_control_end_threshold"] == "80"
    p = make_plan({**A275_HW, "power": {**A275_HW["power"], "battery": True}}, {}, "portatil", "equilibrado")
    assert "preempt=lazy" in p.cmdline and "nmi_watchdog=0" in p.cmdline
