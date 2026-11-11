"""centinela comparar: estadística, orden A-B-B-A, que siempre vuelve al estado original y decisiones."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from centinela import comparar as cp  # noqa: E402


@pytest.fixture(autouse=True)
def estado(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "ESTADO", str(tmp_path / "comparar"))


def test_mann_whitney_exact():
    a, b = [10, 11, 12, 13, 14], [20, 21, 22, 23, 24]
    assert cp.u_stat(a, b) == 25
    assert cp.p_mayor(a, b) == pytest.approx(1 / 252)            # la única permutación tan extrema
    assert cp.p_mayor(b, a) == 1.0
    assert 0.3 < cp.p_mayor([1, 2, 3, 4], [1.5, 2.5, 3.5, 4.5]) < 0.6   # mezclados: nada claro
    assert cp.p_mayor([5, 5, 5], [5, 5, 5]) == 1.0                # empates totales


def test_mann_whitney_large_uses_normal_approximation():
    a = [float(i) for i in range(30)]
    b = [float(i) + 15 for i in range(30)]
    assert cp.p_mayor(a, b) < 0.001


def _filas(**kw):
    base = {"metrica": "x", "nombre": "x", "unidad": "", "sin": 1, "con": 1, "n": [4, 4]}
    return dict(base, **kw)


def test_verdict_needs_clear_and_sizeable_gain_and_no_clear_loss():
    gana = _filas(mejora_pct=6.0, p_mejor=0.01, p_peor=0.99)
    poco = _filas(mejora_pct=1.0, p_mejor=0.01, p_peor=0.99)        # claro pero < 2 %: no vale el cambio
    ruido = _filas(mejora_pct=8.0, p_mejor=0.2, p_peor=0.8)         # grande pero no claro
    pierde = _filas(mejora_pct=-5.0, p_mejor=0.99, p_peor=0.01)
    assert cp.veredicto([gana]) == "mejor"
    assert cp.veredicto([poco]) == "igual" and cp.veredicto([ruido]) == "igual"
    assert cp.veredicto([gana, pierde]) == "peor"                   # gana en algo pero pierde en otra cosa


def test_lower_is_better_metrics_are_inverted():
    filas, ver = cp.comparar_muestras({"planificador.pingpong_us": [30, 31, 32, 33]},
                                      {"planificador.pingpong_us": [20, 21, 22, 23]})
    assert filas[0]["mejora_pct"] > 0 and filas[0]["p_mejor"] < 0.05 and ver == "mejor"


class Fake:
    """Un ajuste cuyo efecto se ve en la prueba: B sube la medida «ia.generacion_ts»."""
    def __init__(self, efecto=5.0, falla_en=None):
        self.valor, self.efecto, self.orden, self.falla_en, self.n = "orig", efecto, [], falla_en, 0

    def ajuste(self):
        return cp.Ajuste("falso", "prueba", lambda: {"v": self.valor}, self._aplicar,
                         lambda s: setattr(self, "valor", s["v"]), pruebas=["p"])

    def _aplicar(self):
        self.valor = "nuevo"

    def prueba(self, _ctx):
        self.n += 1
        if self.falla_en and self.n == self.falla_en:
            raise RuntimeError("se colgó")
        self.orden.append(self.valor)
        base = 10.0 + (self.efecto if self.valor == "nuevo" else 0) + self.n * 0.01   # deriva lenta
        return {"ia.generacion_ts": base}


def run(f, **kw):
    return cp.ejecutar(f.ajuste(), progreso=lambda *_: None, pausa=0, pruebas={"p": f.prueba}, **kw)


def test_abba_order_and_restores_by_default():
    f = Fake()
    r = run(f, rondas=4)
    assert f.orden == ["orig", "nuevo", "nuevo", "orig", "orig", "nuevo", "nuevo", "orig"]
    assert r["veredicto"] == "mejor" and r["aplicado"] is False and f.valor == "orig"   # sin --dejar no se queda


def test_keeps_winner_only_with_dejar_and_reapplies_on_boot(monkeypatch):
    f = Fake()
    r = run(f, rondas=4, dejar=True)
    assert r["aplicado"] is True and f.valor == "nuevo"
    dec = json.loads((Path(cp.ESTADO) / "decisiones.json").read_text())
    assert dec["falso"]["aplicado"] is True
    f.valor = "orig"                                                  # reinicio: sysfs vuelve a su valor
    monkeypatch.setitem(cp.AJUSTES, "falso", f.ajuste())
    assert cp.reaplicar(lambda *_: None) == ["falso"] and f.valor == "nuevo"


def test_loser_is_never_kept_even_with_dejar():
    f = Fake(efecto=-5.0)
    r = run(f, rondas=4, dejar=True)
    assert r["veredicto"] == "peor" and r["aplicado"] is False and f.valor == "orig"


def test_crash_mid_test_restores_original_state():
    f = Fake(falla_en=3)                                              # falla con el ajuste aplicado
    with pytest.raises(RuntimeError):
        run(f, rondas=4, dejar=True)
    assert f.valor == "orig"


def test_kernels_compares_once_enough_boots(monkeypatch):
    seq = iter([30.0, 31.0, 29.5, 22.0, 21.5, 23.0])
    for k in ["6.18-generico"] * 3 + ["6.18-a-medida"] * 3:
        cp.registrar(kernel=k, pruebas={"m": lambda _c: {"planificador.timer_p99_us": 100.0}},
                     arranque=lambda: next(seq))
    r = cp.kernels(minimo=3)
    c = r["comparaciones"][0]
    fila = next(x for x in c["filas"] if x["metrica"] == "arranque.total_s")
    assert fila["mejora_pct"] > 20 and fila["p_mejor"] == pytest.approx(0.05)    # 3 vs 3: p mínimo = 1/20
    assert "falta" in cp.kernels(minimo=4)


def test_thp_selection_parsing():
    assert cp._sel("always [madvise] never") == "madvise"
    assert cp._sel("always defer defer+madvise [madvise] never") == "madvise"
