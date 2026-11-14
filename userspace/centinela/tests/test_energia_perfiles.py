"""Eco y turbo a batería: eco ahorra con el turbo apagado; turbo no pierde velocidad por ahorros de enlace."""
from centinela import apply as ap, energia, sysfs, tuner
from tests.test_tuner import HW


def arbol(tmp_path, monkeypatch):
    def f(rel, v):
        p = tmp_path / rel.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(v)
    f("/sys/class/scsi_host/host0/link_power_management_policy", "x")
    f("/sys/module/pcie_aspm/parameters/policy", "x")
    f("/sys/class/drm/card0/device/power_dpm_state", "x")
    f("/sys/devices/system/cpu/cpufreq/boost", "x")
    f("/proc/sys/vm/dirty_writeback_centisecs", "x")
    f("/proc/sys/vm/stat_interval", "x")
    monkeypatch.setattr(sysfs, "ROOT", str(tmp_path))
    monkeypatch.setattr(energia, "apply_thresholds", lambda act: None)


def cambios(prof, modo):
    act = ap.Actuator(dry_run=True)
    energia.apply(modo, act, prof)
    return {p.split("/")[-1]: v for p, v in act.changes}


def test_turbo_a_bateria_sin_esperas(tmp_path, monkeypatch):
    arbol(tmp_path, monkeypatch)
    c = cambios("rendimiento", "bateria")
    assert c["link_power_management_policy"] == "max_performance" and c["policy"] == "default"
    assert c["power_dpm_state"] == "performance" and c["boost"] == "1"
    assert c["dirty_writeback_centisecs"] == "1500"          # lo que no quita velocidad se mantiene


def test_eco_a_bateria_ahorra(tmp_path, monkeypatch):
    arbol(tmp_path, monkeypatch)
    c = cambios("silencioso", "bateria")
    assert c["link_power_management_policy"] == "med_power_with_dipm" and c["policy"] == "powersave"
    assert c["power_dpm_state"] == "battery" and c["boost"] == "0"
    assert cambios("equilibrado", "corriente")["power_dpm_state"] == "balanced"


def test_eco_usa_balance_power_si_existe():
    hw = {**HW, "cpu": {**HW["cpu"], "epp_choices": ["performance", "balance_performance", "balance_power",
                                                     "power"]}}
    assert tuner.make_plan(hw, {}, "escritorio", "silencioso").cpu["epp"] == "balance_power"
    assert tuner.make_plan(HW, {}, "escritorio", "silencioso").cpu["epp"] == "power"
