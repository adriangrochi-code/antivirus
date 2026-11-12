"""Guía TISMA: lecciones completas, buscador sin tildes, «Probar» solo de una lista fija y API protegida."""
import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_guia import server as s  # noqa: E402
from tisma_guia.lecciones import CATEGORIAS, LECCIONES  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


def test_lecciones_completas_y_simples():
    cats = {c for c, _ in CATEGORIAS}
    ids = [l["id"] for l in LECCIONES]
    assert len(ids) == len(set(ids)) >= 15
    for l in LECCIONES:
        assert l["cat"] in cats and l["titulo"] and l["resumen"] and 2 <= len(l["pasos"]) <= 6, l["id"]
        assert l["probar"] is None or l["probar"] in s.PROBAR, l["id"]
        texto = " ".join(l["pasos"])
        assert "sudo" not in texto and "terminal" not in texto.lower(), l["id"]    # cero terminal
        assert "Windows" not in texto and "Word" not in texto, l["id"]            # sin marcas ajenas
    for c in cats:
        assert any(l["cat"] == c for l in LECCIONES), c


@pytest.mark.parametrize("q, debe", [("wifi", "wifi"), ("CONTRASEÑA wifi", "wifi"), ("imprimir", "imprimir"),
                                      ("cómo instalo un programa", None), ("recuperar archivo borrado", "copias"),
                                      ("virus", "avisos"), ("pendrive", "usb")])
def test_buscador(q, debe):
    r = s.buscar(q)
    if debe:
        assert debe in r, (q, r)


def test_buscar_vacio_devuelve_todo():
    assert len(s.buscar("")) == len(LECCIONES)


@pytest.fixture
def srv(monkeypatch):
    lanzados = []
    monkeypatch.setattr(s, "POPEN", lambda argv, **k: lanzados.append(argv))
    monkeypatch.setattr(s.shutil, "which", lambda x: f"/usr/bin/{x}")
    h = ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    threading.Thread(target=h.serve_forever, daemon=True).start()

    def pedir(metodo, ruta, cuerpo=None, token=s.TOKEN):
        c = http.client.HTTPConnection("127.0.0.1", h.server_port, timeout=5)
        c.request(metodo, ruta, json.dumps(cuerpo) if cuerpo is not None else None,
                  {"X-Token": token, "Host": f"127.0.0.1:{h.server_port}"})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    yield {"pedir": pedir, "lanzados": lanzados}
    h.shutdown()


def test_probar_solo_de_la_lista(srv):
    assert srv["pedir"]("POST", "/api/probar", {"accion": "wifi"})[0] == 200
    assert srv["lanzados"] == [["systemsettings", "kcm_networkmanagement"]]
    assert srv["pedir"]("POST", "/api/probar", {"accion": "rm -rf /"})[0] == 404
    assert srv["pedir"]("POST", "/api/probar", {"accion": "wifi"}, token="x")[0] == 403
    assert len(srv["lanzados"]) == 1


def test_api_lecciones(srv):
    code, d = srv["pedir"]("GET", "/api/lecciones")
    assert code == 200 and len(d["lecciones"]) == len(LECCIONES)
    assert srv["pedir"]("GET", "/img/../../etc/passwd")[0] in (403, 404)


def test_en_el_menu_y_en_la_bienvenida():
    desk = (ROOT / "rootfs/usr/share/applications/net.tisma.Guia.desktop").read_text()
    assert "Exec=tisma-guia" in desk and "Icon=tisma-guia" in desk
    assert 'data-abrir="guia"' in (ROOT / "userspace/bienvenida/tisma_bienvenida/static/index.html").read_text()


def test_inicio_corto_con_temas_que_existen():
    import re
    html = (ROOT / "userspace/guia/tisma_guia/static/index.html").read_text()
    ids = re.findall(r'"(\w+)"', re.search(r"PRINCIPALES = \[(.*?)\]", html).group(1))
    assert 4 <= len(ids) <= 8
    assert set(ids) <= {l["id"] for l in LECCIONES}
