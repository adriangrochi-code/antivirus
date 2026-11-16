"""hope.api v0.1: las 7 herramientas certificadas en el kit de cálculo, con su verificación a la vista."""
import pytest

hb = pytest.importorskip("tisma_code.hope_bridge")
if hb.hope_api is None:
    pytest.skip("este Hope no tiene hope.api", allow_module_level=True)


def test_esquemas_y_kit():
    from tisma_code import kits, tools
    nombres = [s["function"]["name"] for s in hb.esquemas_certificados()]
    assert sorted(nombres) == sorted(hb.CERTIFICADAS)
    assert not set(nombres) & tools.TOOL_NAMES                      # sin choques con las de TISMA
    assert set(hb.CERTIFICADAS) <= kits.KITS["calculo"]["tools"]


def test_certificada_y_aviso_no_verificado(tmp_path):
    h = hb.HopeTools(tmp_path)
    out, ok = h.certificada("equivalencia", {"a": "(x+1)**2", "b": "x**2+2*x+1"})
    assert ok and "[verificado]" in out
    assert h.certificada("python", {"entrada": "print(1)"})[1] is False        # solo las certificadas

    class R:
        ok, verificado = True, False
        def texto(self):
            return "x = 3 [NO verificado]"
    h._api = type("A", (), {"ejecutar": lambda self, n, a: R()})()
    out, ok = h.certificada("raiz", {})
    assert ok and "no lo des por cierto" in out
