"""Bienvenida TISMA: autenticación, validación de entradas y órdenes fijas."""
import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_bienvenida import server  # noqa: E402


@pytest.fixture
def srv(monkeypatch, tmp_path):
    calls = []

    class R:
        returncode, stdout, stderr = 0, "active", ""
    monkeypatch.setattr(server, "RUN", lambda argv, **k: calls.append(argv) or R())
    monkeypatch.setattr(server, "POPEN", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(server, "CONF", tmp_path / "tisma" / "bienvenida.json")
    monkeypatch.setattr(server, "current_scheme", lambda: "CentinelaAero")
    apps = tmp_path / "apps"
    apps.mkdir()
    for n in ("org.kde.dolphin", "chromium", "thunderbird", "libreoffice-startcenter", "org.kde.elisa"):
        (apps / f"{n}.desktop").write_text("[Desktop Entry]\n")
    monkeypatch.setattr(server, "APPS_DIR", apps)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_port, calls
    httpd.shutdown()


def req(port, method, path, body=None, token=True, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}", "Content-Type": "application/json"}
    if token:
        h["X-Token"] = server.TOKEN
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    return r.status, r.read()


def test_auth(srv):
    port, _ = srv
    assert req(port, "GET", "/api/estado", token=False)[0] == 403
    assert req(port, "GET", "/api/estado", host="evil:1")[0] == 403
    assert req(port, "GET", "/")[0] == 403                                  # la página exige el token en la URL
    assert req(port, "GET", f"/?t={server.TOKEN}")[0] == 403                # el de la API no abre la página
    server.BOOT_USED.clear()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", f"/?t={server.BOOT}", headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    r.read()
    assert r.status == 200 and "HttpOnly" in r.getheader("Set-Cookie")
    assert req(port, "GET", f"/?t={server.BOOT}")[0] == 403                 # la URL del argv: un solo uso
    c.request("GET", "/", headers={"Host": f"127.0.0.1:{port}", "Cookie": f"bv={server.TOKEN}"})
    r = c.getresponse()
    r.read()
    assert r.status == 200                                                  # recargar la ventana sigue funcionando


def test_inputs_validated_and_commands_fixed(srv):
    port, calls = srv
    for path, body in (("/api/tema", {"modo": "rosa", "acento": "violeta"}),
                       ("/api/tema", {"modo": "claro", "acento": "#000; rm -rf ~"}),
                       ("/api/perfil", {"perfil": "root"}), ("/api/abrir", {"app": "bash"})):
        assert req(port, "POST", path, body)[0] == 400
    assert calls == []
    assert req(port, "POST", "/api/tema", {"modo": "claro", "acento": "verde"})[0] == 200
    assert calls[:2] == [["plasma-apply-colorscheme", "TismaClaro"],
                         ["plasma-apply-colorscheme", "--accent-color", "#2FB37A"]]
    st, body = req(port, "POST", "/api/perfil", {"perfil": "diario"})
    r = json.loads(body)
    assert r["lanzadores"] == ["applications:org.kde.dolphin.desktop", "applications:chromium.desktop",
                               "applications:thunderbird.desktop", "applications:libreoffice-startcenter.desktop",
                               "applications:org.kde.elisa.desktop"]               # la tienda no existe aquí: se omite
    gd = next(c for c in calls if c[0] == "gdbus")
    assert gd[-2] == "org.kde.PlasmaShell.evaluateScript" and json.dumps(r["lanzadores"]) in gd[-1]
    assert ["systemctl", "start", "--no-block", "centinela-perfil@equilibrado.service"] in calls
    assert json.loads(req(port, "GET", "/api/estado")[1])["perfil"] == "diario"


def test_listo_marks_done(srv, monkeypatch):
    port, _ = srv
    assert req(port, "POST", "/api/listo", {})[0] == 200
    assert server.read_conf()["hecha"] is True
    assert server.main(["--si-primera-vez"]) == 0                         # ya hecha: no vuelve a abrirse


def test_first_login_shows_once_even_if_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONF", tmp_path / "b.json")
    started = []
    monkeypatch.setattr(server, "ThreadingHTTPServer", lambda *a: started.append(a) or (_ for _ in ()).throw(SystemExit))
    with pytest.raises(SystemExit):
        server.main(["--si-primera-vez", "--no-browser"])          # primera vez: se abre
    assert server.read_conf()["mostrada"] is True
    assert server.main(["--si-primera-vez"]) == 0                  # cerrada con la X: no vuelve en cada inicio
