"""Modo sin apuro: cola entre procesos, estimación con disco y trabajador que pausa a batería."""
import json

import pytest

from tisma_code import models, sin_apuro as sa


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    md = tmp_path / "models"
    md.mkdir()
    monkeypatch.setattr(models, "MODELS_DIR", md)
    monkeypatch.setattr(sa, "DIR", tmp_path / "sin-apuro")
    monkeypatch.setattr(sa, "COLA", tmp_path / "sin-apuro" / "tareas.json")
    monkeypatch.setattr(sa, "CERROJO", tmp_path / "sin-apuro" / ".trabajando")
    m = md / "grande-70b-Q4_K_M.gguf"
    m.write_bytes(b"GGUF")
    return m


def test_estimar_lee_del_disco_si_no_entra(entorno, monkeypatch):
    from tisma_code import motor
    hw = {"ram_mb": 16000, "ram_bandwidth_gbs": 7}
    monkeypatch.setattr(motor, "total_size", lambda p: 4_000_000_000)
    chico = sa.estimar(str(entorno), {**hw, "disco_mbs": 400})
    assert chico["cabe"] and chico["tokens_s"] > 0.5
    monkeypatch.setattr(motor, "total_size", lambda p: 40_000_000_000)
    grande = sa.estimar(str(entorno), {**hw, "disco_mbs": 400})
    assert not grande["cabe"] and grande["tokens_s"] < 0.05          # el disco manda: decenas de s por palabra
    assert "disco" in sa.texto_estimacion(grande) and "h" in sa.texto_estimacion(grande)


def test_cola_valida_y_no_expone_adjunto(entorno, tmp_path):
    with pytest.raises(ValueError):
        sa.agregar("hola", str(tmp_path / "fuera.gguf"))
    with pytest.raises(ValueError):
        sa.agregar("   ", str(entorno))
    t = sa.agregar("Resume esto", str(entorno), adjunto="texto largo", nombre_adjunto="a.md")
    lista = sa.listar()
    assert lista[0]["id"] == t["id"] and "adjunto" not in lista[0]
    assert oct((sa.COLA).stat().st_mode & 0o777) == "0o600"
    assert sa.cancelar(t["id"]) and sa.listar()[0]["estado"] == "cancelada"
    assert sa.borrar(t["id"]) and sa.listar() == []
    with pytest.raises(ValueError):
        sa.resultado("../../etc/passwd")


def test_contexto_y_mensajes(entorno):
    t = sa.agregar("Revisa", str(entorno), adjunto="x" * 60_000, nombre_adjunto="trabajo.txt")
    assert sa.contexto_para(t) % 1024 == 0 and sa.contexto_para(t) >= 20_000
    assert "trabajo.txt" in sa.mensajes(t)[1]["content"]


class FakeProc:
    pid = 999999

    def poll(self):
        return None


class FakeMotor:
    def __init__(self):
        self.port, self.api_key, self.proc = 1, "k", None

    def start(self, path, hw, ctx=0, extra=None):
        assert "--mlock" not in extra
        self.proc = FakeProc()
        return "http://127.0.0.1:1/v1"

    def stop(self):
        self.proc = None


def test_trabajador_hace_la_cola_y_avisa(entorno, monkeypatch):
    avisos = []
    monkeypatch.setattr(sa, "enchufado", lambda: True)
    monkeypatch.setattr(sa, "argumentos", lambda p, hw, c: ["-ngl", "0"])
    monkeypatch.setattr(models, "port_listening", lambda p: True)
    monkeypatch.setattr(sa, "avisar", lambda *a: avisos.append(a))
    monkeypatch.setattr(sa.time, "sleep", lambda s: None)
    monkeypatch.setattr(sa, "MotorLento", FakeMotor)
    monkeypatch.setattr(sa, "generar", lambda url, k, t, pensar, av: "Resumen listo en tres frases.")
    t = sa.agregar("Resume", str(entorno))
    assert sa.trabajar(hw={"ram_mb": 16000}) == 1
    assert sa.listar()[0]["estado"] == "lista" and sa.resultado(t["id"]).startswith("Resumen listo")
    assert avisos and "lista" in avisos[0][0]


def test_a_bateria_espera(entorno, monkeypatch):
    monkeypatch.setattr(sa, "enchufado", lambda: False)
    esperas = []

    def espera(s):
        esperas.append(s)
        sa.cancelar(t["id"])                      # para salir del bucle tras la primera espera

    t = sa.agregar("Resume", str(entorno))
    assert sa.trabajar(hw={"ram_mb": 16000}, espera=espera) == 0
    assert esperas == [60]


def test_un_solo_trabajador(entorno):
    import fcntl
    import os
    sa.DIR.mkdir(parents=True)
    fd = os.open(sa.CERROJO, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    sa.agregar("Resume", str(entorno))
    try:
        assert sa.trabajar(hw={}) == 0              # otro ya trabaja: sale sin tocar nada
        assert sa.listar()[0]["estado"] == "en_cola"
    finally:
        os.close(fd)


def test_generar_lee_el_flujo(monkeypatch):
    class R:
        def __enter__(self):
            return iter([b'data: {"choices":[{"delta":{"content":"Hola"}}]}\n',
                         b'data: {"choices":[{"delta":{"content":" mundo"}}]}\n', b"data: [DONE]\n"])

        def __exit__(self, *a):
            return False
    cuerpos = []
    monkeypatch.setattr(sa.urllib.request, "urlopen", lambda req, timeout=None: (cuerpos.append(req.data), R())[1])
    vistos = []
    assert sa.generar("http://127.0.0.1:1/v1", "k", {"pedido": "x"}, False, vistos.append) == "Hola mundo"
    assert json.loads(cuerpos[0])["chat_template_kwargs"] == {"enable_thinking": False} and vistos[-1] == "Hola mundo"
