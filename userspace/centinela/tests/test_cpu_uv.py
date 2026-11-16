"""Undervolt de CPU: el mínimo estable CON el mismo rendimiento, un paso de margen, y nada si no está disponible."""
from centinela import autotune
from centinela.autotune import StabilityResult


def _prueba(falla_desde=None, lento_desde=None):
    estado = {"pasos": 0}
    orig = autotune.set_cpu_uv

    def aplicar(act, v):
        estado["pasos"] = v
        orig(act, v)

    def test(seconds):
        v = estado["pasos"]
        if falla_desde is not None and v >= falla_desde:
            return StabilityResult(False, "3 errores de cálculo", 70, {"trabajo_por_s": 100})
        trabajo = 90 if lento_desde is not None and v >= lento_desde else 100
        return StabilityResult(True, "", 70 - v * 0.3, {"trabajo_por_s": trabajo, "vatios": 15 - v * 0.2})
    return aplicar, test


def test_minimo_estable_con_margen(tmp_path, monkeypatch):
    monkeypatch.setattr(autotune, "PROGRESS", tmp_path / "p.json")
    monkeypatch.setattr(autotune, "MARKER", tmp_path / "m.json")
    aplicar, test = _prueba(falla_desde=11)
    monkeypatch.setattr(autotune, "set_cpu_uv", aplicar)
    r = autotune.run_cpu_uv(5, dry_run=True, test=test, disponible=lambda: (True, ""))
    assert r["estado"] == "terminado" and r["resultado"]["cpu_uv_pasos"] == 9      # falló el 11: 10 - 1 de margen
    assert r["resultado"]["cpu_uv_mv"] == -56.25 and r["ahorro"]["vatios"] == 1.8


def test_rinde_menos_no_vale(tmp_path, monkeypatch):
    monkeypatch.setattr(autotune, "PROGRESS", tmp_path / "p.json")
    monkeypatch.setattr(autotune, "MARKER", tmp_path / "m.json")
    aplicar, test = _prueba(lento_desde=6)
    monkeypatch.setattr(autotune, "set_cpu_uv", aplicar)
    r = autotune.run_cpu_uv(5, dry_run=True, test=test, disponible=lambda: (True, ""))
    assert r["resultado"]["cpu_uv_pasos"] == 4
    assert any("rinde 10 % menos" in p["motivo"] for p in r["pasos"])


def test_no_disponible(tmp_path, monkeypatch):
    monkeypatch.setattr(autotune, "PROGRESS", tmp_path / "p.json")
    r = autotune.run_cpu_uv(5, dry_run=True, disponible=lambda: (False, "Falta registrar la clave"))
    assert r["estado"] == "no disponible" and "clave" in r["motivo"]


def test_motivos_en_simple(monkeypatch):
    monkeypatch.setattr(autotune, "_cpu_compatible", lambda: True)
    monkeypatch.setattr(autotune.sysfs, "read", lambda p, d=None: None)
    class R:
        def __init__(self, err):
            self.stderr, self.returncode = err, 1
    ok, m = autotune.cpu_uv_disponible(lambda *a, **k: R("modprobe: ERROR: could not insert 'tisma_uv': Key was rejected by service"))
    assert not ok and "Enroll MOK" in m
    ok, m = autotune.cpu_uv_disponible(lambda *a, **k: R("modprobe: FATAL: Module tisma_uv not found in directory"))
    assert not ok and "se actualizó" in m
    monkeypatch.setattr(autotune, "_cpu_compatible", lambda: False)
    assert "ThinkPad A275" in autotune.cpu_uv_disponible()[1]
