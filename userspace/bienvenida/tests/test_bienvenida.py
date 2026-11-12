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
    monkeypatch.setattr(server, "current_scheme", lambda: "TismaCristal")
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


def test_profile_apps_install_with_pkexec_and_report_progress(srv, monkeypatch, tmp_path):
    port, calls = srv
    estado = tmp_path / "estado.json"
    monkeypatch.setattr(server, "PERFIL_ESTADO", estado)
    monkeypatch.setattr(server, "INSTALANDO", {})

    class P:
        rc = None

        def poll(self):
            return self.rc
    proc = P()
    monkeypatch.setattr(server, "POPEN", lambda argv, **k: calls.append(argv) or proc)
    monkeypatch.setattr(server, "live_session", lambda: False)
    assert req(port, "POST", "/api/instalar", {"perfil": "juegos; rm -rf /"})[0] == 400
    assert req(port, "GET", "/api/faltan?perfil=../x")[0] == 400
    assert json.loads(req(port, "GET", "/api/instalacion")[1])["fase"] == "ninguna"
    assert json.loads(req(port, "POST", "/api/instalar", {"perfil": "juegos"})[1])["ok"] is True
    assert ["pkexec", "tisma-perfil", "instalar", "juegos", "creacion"] in calls
    assert json.loads(req(port, "POST", "/api/instalar", {"perfil": "diario"})[1])["ok"] is False   # una a la vez
    st = json.loads(req(port, "GET", "/api/instalacion")[1])
    assert st["fase"] == "esperando permiso" and st["activo"] is True
    import time as _t
    estado.write_text(json.dumps({"fase": "instalando", "inicio": _t.time(), "paquetes": 17, "ok": None}))
    st = json.loads(req(port, "GET", "/api/instalacion")[1])
    assert st["fase"] == "instalando" and st["paquetes"] == 17
    proc.rc = 126                                                        # cerró la ventana de contraseña
    estado.unlink()
    assert json.loads(req(port, "GET", "/api/instalacion")[1])["fase"] == "cancelado"
    monkeypatch.setattr(server, "live_session", lambda: True)            # live: no se instala en la RAM
    r = json.loads(req(port, "POST", "/api/instalar", {"perfil": "diario"})[1])
    assert r["ok"] is False and "live" in r["error"]


def test_saludo_con_el_nombre_de_pila():
    from tisma_bienvenida import server
    assert server.nombre_de_pila("Ana Prueba,,,", "anap") == "Ana"
    assert server.nombre_de_pila("", "anap") == "anap"
    assert server.nombre_de_pila(",,,", "anap") == "anap"


def test_varios_usos_a_la_vez(srv, monkeypatch):
    """Juegos + programación + …: se suman las listas de apps y se instala todo junto."""
    port, calls = srv
    c = server.combinar(["diario", "juegos", "programacion"])
    assert c["listas"] == ["esenciales", "diario", "juegos", "creacion", "programacion"]
    assert c["potencia"] == "rendimiento"                          # alguno lo pide
    assert len(c["apps"]) <= server.MAX_ANCLADAS and len(set(c["apps"])) == len(c["apps"])
    todo = server.combinar(list(server.PERFILES))
    assert set(todo["listas"]) == {x for p in server.PERFILES.values() for x in p["listas"]}
    # La API acepta varios, rechaza los que no existen y los repetidos no cuentan dos veces
    assert req(port, "POST", "/api/perfil", {"perfiles": ["diario", "root"]})[0] == 400
    assert req(port, "POST", "/api/perfil", {"perfiles": []})[0] == 400
    assert req(port, "POST", "/api/perfil", {"perfiles": ["diario", "juegos", "diario"]})[0] == 200
    est = json.loads(req(port, "GET", "/api/estado")[1])
    assert est["perfiles_elegidos"] == ["diario", "juegos"]
    assert ["systemctl", "start", "--no-block", "centinela-perfil@rendimiento.service"] in calls
    monkeypatch.setattr(server, "live_session", lambda: False)
    monkeypatch.setattr(server, "INSTALANDO", {})
    assert json.loads(req(port, "POST", "/api/instalar", {"perfiles": ["diario", "juegos"]})[1])["ok"]
    inst = next(x for x in calls if x[:3] == ["pkexec", "tisma-perfil", "instalar"])
    assert inst[3:] == ["esenciales", "diario", "juegos", "creacion"]


def test_escritorio_simple_para_chicos(monkeypatch, tmp_path):
    """Equipo de un estudiante de primaria: barra, iconos y letra más grandes; reversible y nunca con usos de adulto."""
    monkeypatch.setattr(server, "CONF", tmp_path / "b.json")
    assert server.es_equipo_de_chicos(["primaria"]) and server.es_equipo_de_chicos(["primaria", "diario"])
    assert not server.es_equipo_de_chicos(["primaria", "programacion"]) and not server.es_equipo_de_chicos(["liceo"])
    assert "gridUnit * 3.6" in server.panel_script(["applications:x.desktop"], 3.6)
    assert "height" not in server.panel_script(["applications:x.desktop"])
    llamadas = []
    monkeypatch.setattr(server, "_ok", lambda argv, timeout=60: llamadas.append(argv) or (True, ""))
    assert server.escritorio_chicos(True) == {"ok": True, "chicos": True}
    assert any("Inter,12," in " ".join(a) for a in llamadas) and server.read_conf()["chicos"] is True
    llamadas.clear()
    server.escritorio_chicos(False)
    assert any("Inter,10," in " ".join(a) for a in llamadas) and any("gridUnit * 2.6" in " ".join(a) for a in llamadas)


def test_tema_viejo_pasa_a_tisma_cristal(monkeypatch):
    llamadas = []
    monkeypatch.setattr(server, "_ok", lambda argv, timeout=60: llamadas.append(argv) or (True, ""))
    monkeypatch.setattr(server, "current_scheme", lambda: "CentinelaAero")
    assert server.migrar_tema() and llamadas == [["plasma-apply-colorscheme", "TismaCristal"]]
    monkeypatch.setattr(server, "current_scheme", lambda: "TismaCristal")
    llamadas.clear()
    assert not server.migrar_tema() and not llamadas
