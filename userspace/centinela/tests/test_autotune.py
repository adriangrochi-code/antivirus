import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from centinela import apply, autotune, sysfs  # noqa: E402
from centinela.autotune import StabilityResult  # noqa: E402


def test_search_keeps_one_step_margin():
    applied = []
    stable, hist = autotune.search([2, 4, 6, 8, 10], applied.append, lambda v: StabilityResult(v < 8, "" if v < 8 else "errores"))
    assert stable == 4 and applied[-1] == 4                   # falló 8, pasó 6 → se queda en 4 (margen)
    stable, _ = autotune.search([2, 4], lambda v: None, lambda v: StabilityResult(True))
    assert stable == 4                                        # todo estable: sin margen
    stable, _ = autotune.search([2, 4], lambda v: None, lambda v: StabilityResult(False, "x"))
    assert stable == 0                                        # ni el primer paso: fábrica
    stable, hist = autotune.search([2, 4, 6], lambda v: None, lambda v: StabilityResult(True), known_bad={6})
    assert stable == 2 and "anterior" in hist[-1]["motivo"]   # nunca reintenta un valor que colgó el equipo


@pytest.fixture
def fake(tmp_path, monkeypatch):
    root = tmp_path / "root"
    dev = root / "sys/class/drm/card0/device"
    dev.mkdir(parents=True)
    (dev / "pp_sclk_od").write_text("0")
    (dev / "pp_od_clk_voltage").write_text("OD_SCLK:\n0: 500Mhz\n1: 2400Mhz\nOD_VDDGFX_OFFSET:\n0mV\n")
    (root / "sys/devices/system/cpu/cpufreq").mkdir(parents=True)
    (root / "sys/devices/system/cpu/cpufreq/boost").write_text("0")
    state = tmp_path / "state"
    monkeypatch.setattr(sysfs, "ROOT", str(root))
    monkeypatch.setattr(apply, "STATE_DIR", str(state))
    monkeypatch.setattr(autotune, "MARKER", state / "autotune-en-prueba.json")
    monkeypatch.setattr(autotune, "PROGRESS", tmp_path / "progreso.json")
    return root, state, tmp_path


def test_full_run_with_simulated_hardware(fake):
    root, state, tmp = fake
    current = {"uv": 0}
    orig = autotune.set_gpu_uv

    def spy(card, method, mv, base=None, dry_run=False):
        current["uv"] = mv
        return orig(card, method, mv, base, dry_run)
    autotune.set_gpu_uv = spy

    def test(seconds):
        od = int((root / "sys/class/drm/card0/device/pp_sclk_od").read_text())
        unstable = od >= 10 or current["uv"] <= -60
        return StabilityResult(not unstable, "errores de cálculo" if unstable else "", 70.0)

    try:
        r = autotune.run(seconds=10, test=test)
    finally:
        autotune.set_gpu_uv = orig
    assert r["resultado"]["turbo"] is True
    assert r["resultado"]["gpu_oc_card0"] == 6                # 10 falla, 8 pasa → margen → 6
    assert r["resultado"]["gpu_uv_mv_card0"] == -40           # -60 falla, -50 pasa → margen → -40
    assert (root / "sys/devices/system/cpu/cpufreq/boost").read_text() == "1"
    assert not autotune.MARKER.exists()
    assert json.loads((state / "prefs.json").read_text())["autotune"]["gpu_oc_card0"] == 6
    assert json.loads((tmp / "progreso.json").read_text())["estado"] == "terminado"
    assert "lockdown" in r["aviso_cpu_uv"]


def test_crash_during_test_restores_stock(fake):
    root, state, _ = fake
    state.mkdir(parents=True, exist_ok=True)
    autotune.MARKER.write_text(json.dumps({"ajuste": "oc card0", "valor": 12}))
    (state / "prefs.json").write_text(json.dumps({"autotune": {"gpu_oc_card0": 12}}))
    (root / "sys/class/drm/card0/device/pp_sclk_od").write_text("12")
    msg = autotune.boot_apply()
    assert "se colgó" in msg and not autotune.MARKER.exists()
    assert (root / "sys/class/drm/card0/device/pp_sclk_od").read_text().strip() == "0"
    assert json.loads((state / "autotune-inestables.json").read_text()) == {"oc card0": [12]}
    assert "autotune" not in json.loads((state / "prefs.json").read_text())


def test_marker_written_before_applying_each_step():
    order = []
    autotune.search([2, 4], lambda v: order.append(("aplicar", v)), lambda v: StabilityResult(v < 4, "x"),
                    before=lambda v: order.append(("marca", v)))
    assert order[:4] == [("marca", 2), ("aplicar", 2), ("marca", 4), ("aplicar", 4)]


def test_run_restores_factory_values_on_error(monkeypatch):
    resets = []
    monkeypatch.setattr(autotune, "capabilities", lambda: {"gpu_oc": ["/sys/class/drm/card0"], "gpu_uv": {},
                                                            "turbo": False, "cpu_uv": False, "cpu_uv_motivo": ""})
    monkeypatch.setattr(autotune, "gpu_cards", lambda: ["/sys/class/drm/card0"])
    monkeypatch.setattr(autotune, "reset_gpu", lambda c, dry_run=False: resets.append(c))
    monkeypatch.setattr(autotune, "set_gpu_oc", lambda act, c, v: None)
    monkeypatch.setattr(autotune, "write_progress", lambda d: None)

    def boom(seconds):
        raise FileNotFoundError("centinela-estres")
    try:
        autotune.run(10, dry_run=True, test=boom)
        raise AssertionError("debe propagar el error")
    except FileNotFoundError:
        pass
    assert resets == ["/sys/class/drm/card0"]
