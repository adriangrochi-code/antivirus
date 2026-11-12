"""TISMA Seguridad: todo con botones, solo tareas de una lista fija, una a la vez, resúmenes en lenguaje simple."""
import http.client
import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_seguridad_panel import server as s  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


class Proc:
    def __init__(self, argv, lineas=("ok",), rc=0, **k):
        import io
        self.argv, self.stdout, self.returncode, self._rc = argv, io.StringIO("".join(f"{x}\n" for x in lineas)), None, rc

    def poll(self):
        return self.returncode

    def wait(self):
        self.returncode = self._rc
        return self._rc


@pytest.fixture
def srv(monkeypatch):
    lanzados = []
    monkeypatch.setattr(s, "POPEN", lambda argv, **k: lanzados.append(argv) or Proc(argv, s._salida_falsa))
    s._salida_falsa = ("== Análisis ==", "/home/x/a.exe: Win.Trojan FOUND")
    s.JOB.update(proc=None, tarea=None, salida=[], inicio=0.0, codigo=None)
    h = ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    threading.Thread(target=h.serve_forever, daemon=True).start()
    port = h.server_port

    def pedir(metodo, ruta, cuerpo=None, token=s.TOKEN, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(metodo, ruta, json.dumps(cuerpo) if cuerpo is not None else None,
                  {"X-Token": token, "Host": host or f"127.0.0.1:{port}", "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    yield {"pedir": pedir, "lanzados": lanzados}
    h.shutdown()


def esperar(pedir):
    for _ in range(100):
        d = pedir("GET", "/api/tarea")[1]
        if not d["corriendo"] and d.get("resumen"):
            return d
        time.sleep(0.02)
    raise AssertionError("la tarea no terminó")


def test_un_boton_lanza_una_orden_fija_con_pkexec(srv):
    assert srv["pedir"]("POST", "/api/tarea", {"tarea": "virus"})[0] == 200
    d = esperar(srv["pedir"])
    assert srv["lanzados"][0][:3] == ["pkexec", "/usr/bin/tisma-seguridad", "virus"]
    assert d["resumen"] == {"nivel": "aviso", "texto": d["resumen"]["texto"]} and "1 archivo" in d["resumen"]["texto"]


def test_solo_tareas_de_la_lista_y_datos_validados(srv):
    assert srv["pedir"]("POST", "/api/tarea", {"tarea": "rm -rf /"})[0] == 409
    assert srv["pedir"]("POST", "/api/tarea", {"tarea": "usb_permitir", "id": "3; reboot"})[0] == 409
    assert srv["pedir"]("POST", "/api/tarea", {"tarea": "usb_permitir", "id": "12"})[0] == 200
    esperar(srv["pedir"])
    assert srv["lanzados"] == [["pkexec", "/usr/bin/tisma-seguridad", "usb-permitir", "12"]]


def test_sin_token_o_con_otro_host_no_hace_nada(srv):
    assert srv["pedir"]("POST", "/api/tarea", {"tarea": "virus"}, token="x")[0] == 403
    assert srv["pedir"]("GET", "/api/tareas", host="evil.example:80")[0] == 403
    assert not srv["lanzados"]


def test_una_tarea_a_la_vez(srv, monkeypatch):
    class Lento(Proc):
        def wait(self):
            time.sleep(0.3)
            return super().wait()
    monkeypatch.setattr(s, "POPEN", lambda argv, **k: Lento(argv))
    assert srv["pedir"]("POST", "/api/tarea", {"tarea": "rootkits"})[0] == 200
    code, r = srv["pedir"]("POST", "/api/tarea", {"tarea": "virus"})
    assert code == 409 and "en curso" in r["error"]


def test_las_peligrosas_piden_confirmacion_y_las_preguntas_de_terminal_no_existen(srv):
    t = srv["pedir"]("GET", "/api/tareas")[1]
    assert t["usb_cerrar"]["confirmar"] and t["estricto_on"]["confirmar"] and not t["virus"]["confirmar"]
    assert s.TAREAS["usb_cerrar"][1]({})[-1] == "--si" and s.TAREAS["estricto_on"][1]({})[-1] == "--si"
    script = (ROOT / "rootfs/usr/bin/tisma-seguridad").read_text()
    assert 'read -r -p "¿Continuar? [s/N] "' not in script and "confirmar()" in script


@pytest.mark.parametrize("tarea, salida, rc, nivel, frase", [
    ("virus", ["Scanned files: 30"], 0, "ok", "No se encontraron virus"),
    ("rootkits", ["Warning: /usr/bin/x changed"], 0, "aviso", "1 punto"),
    ("auditoria", ["  Hardening index : 83 [#####]"], 0, "ok", "83 de 100"),
    ("auditoria", ["  Hardening index : 55 [##]"], 0, "aviso", "recomendaciones"),
    ("integridad", ["⚠ Hay archivos del sistema modificados: revisa x"], 0, "aviso", "cambiaron"),
    ("secureboot", ["  2. Escribe la contraseña: 48151623"], 0, "ok", "48151623"),
    ("secureboot", ["La clave TISMA ya está registrada."], 0, "ok", "ya está registrada"),
    ("red", ["x"], 126, "error", "permiso de administrador"),
    ("drivers", ["x"], 1, "error", "problema"),
])
def test_resumenes_en_lenguaje_simple(tarea, salida, rc, nivel, frase):
    r = s.resumir(tarea, salida, rc)
    assert r["nivel"] == nivel and frase in r["texto"]


def test_esta_en_el_menu_y_con_su_permiso():
    desk = (ROOT / "rootfs/usr/share/applications/net.tisma.Seguridad.desktop").read_text()
    assert "Exec=tisma-seguridad-panel" in desk and "Icon=tisma-seguridad" in desk
    pol = (ROOT / "rootfs/usr/share/polkit-1/actions/org.tisma.seguridad.policy").read_text()
    for exe in ("/usr/bin/tisma-seguridad", "/usr/bin/escudo", "/usr/bin/tisma-secureboot"):
        assert f'exec.path">{exe}<' in pol
