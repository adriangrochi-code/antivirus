"""Analizar los programas al terminar la descarga, antes del primer clic."""
import os
import threading
import time

from escudo import detonar, primera_ejecucion as pe


class Inf:
    def __init__(self, v):
        self.veredicto, self.motivos, self.entorno, self.segundos, self.nota = v, [], "vm", 7.0, ""

    def to_dict(self):
        return {"veredicto": self.veredicto, "motivos": [], "entorno": "vm"}


def puerta(monkeypatch, tmp_path, veredicto="limpio"):
    guardados = {}
    monkeypatch.setattr(pe, "DET_DIR", tmp_path / "det")
    monkeypatch.setattr(pe, "ocupado", lambda: False)
    monkeypatch.setattr(detonar, "cargar_permitidos", lambda: dict(guardados))
    monkeypatch.setattr(detonar, "guardar_veredicto",
                        lambda inf, path: guardados.__setitem__(detonar.sha256_file(path), {"veredicto": inf.veredicto}))
    monkeypatch.setattr(pe.informe, "guardar", lambda *a, **k: None)
    monkeypatch.setattr(pe.informe, "explicar", lambda d: {"grupos": []})
    llamadas = []
    monkeypatch.setattr(detonar, "detonar", lambda path, **k: (llamadas.append((path, k.get("fondo"))), Inf(veredicto))[1])
    alertas = []
    g = pe.Gate(alert=alertas.append)
    return g, alertas, llamadas


def descarga(tmp_path, datos=b"\x7fELF" + b"\0" * 100, nombre="juego"):
    d = tmp_path / "home" / "ana" / "Descargas"
    d.mkdir(parents=True, exist_ok=True)
    p = d / nombre
    p.write_bytes(datos)
    os.setxattr(p, "user.xdg.origin.url", b"https://ejemplo.org/juego")
    return str(p)


def esperar(g):
    for _ in range(100):
        if not g._queued:
            return
        time.sleep(0.05)


def test_candidatos(tmp_path):
    st = os.stat(__file__)
    assert not pe.candidato(__file__, b"impo", st)           # ni programa ni descarga
    p = descarga(tmp_path)
    assert pe.candidato(p, b"\x7fELF", os.stat(p))
    assert pe.candidato(p, b"MZ\x90\0", os.stat(p))
    assert not pe.candidato(p, b"%PDF", os.stat(p))


def test_limpio_en_silencio_y_queda_permitido(tmp_path, monkeypatch):
    g, alertas, llamadas = puerta(monkeypatch, tmp_path)
    p = descarga(tmp_path)
    assert g.precalentar(p)
    esperar(g)
    assert llamadas == [(p, True)] and alertas == []          # de antemano: con prioridad baja
    sha = detonar.sha256_file(p)
    assert pe.decide(p, True, sha, g.allow) == "permitir"           # el primer clic ya no espera
    assert not g.precalentar(p)                                     # no se analiza dos veces


def test_peligroso_avisa(tmp_path, monkeypatch):
    g, alertas, _ = puerta(monkeypatch, tmp_path, "sospechoso")
    g.precalentar(descarga(tmp_path))
    esperar(g)
    assert len(alertas) == 1 and alertas[0].severity == "medium"


def test_no_programas_no_se_analizan(tmp_path, monkeypatch):
    g, _, llamadas = puerta(monkeypatch, tmp_path)
    assert not g.precalentar(descarga(tmp_path, b"%PDF-1.7 hola", "doc.pdf"))
    assert llamadas == []


def test_clic_durante_el_analisis_avisa_y_luego_da_el_resultado(tmp_path, monkeypatch):
    g, alertas, _ = puerta(monkeypatch, tmp_path)
    puede = threading.Event()
    monkeypatch.setattr(detonar, "detonar", lambda path, **k: (puede.wait(5), Inf("limpio"))[1])
    p = descarga(tmp_path)
    g.precalentar(p)
    sha = detonar.sha256_file(p)
    # lo que hace on_exec con un archivo que se está analizando de antemano
    assert pe.decide(p, True, sha, g.allow) == "analizar" and sha in g._queued
    g._pidieron.add(sha)
    puede.set()
    esperar(g)
    assert alertas and "sin comportamiento malicioso" in alertas[-1].detail


def test_el_antivirus_lo_pasa_a_la_puerta_solo_si_no_es_malware(tmp_path):
    from escudo import monitor
    from escudo.scanner import Detection

    class S:
        def __init__(self, dets):
            self.dets = dets

        def _excluded(self, p):
            return False

        def scan_file(self, p):
            return self.dets
    f = tmp_path / "x"
    f.write_bytes(b"\x7fELF")
    vistos = []
    rtp = monitor.RealTimeProtection(S([]), quarantine=None)
    rtp.al_revisar = vistos.append
    rtp.check(str(f))
    assert vistos == [str(f)]
    rtp = monitor.RealTimeProtection(S([Detection(str(f), "Firma", "high", "firma")]), quarantine=None,
                                     auto_quarantine=False)
    rtp.al_revisar = vistos.append
    rtp.check(str(f))
    assert vistos == [str(f)]                                  # malware conocido: ya avisado, no se detona


def test_prioridad_baja_pero_no_idle(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "x")
    monkeypatch.setattr(detonar.shutil, "which", lambda n: "/usr/bin/" + n)
    c = detonar.como_usuario(["prog"], 1000, 1000, 50, fondo=True)
    assert "--slice=tisma-segundo-plano.slice" in c and "CPUWeight=20" in c and "CPUWeight=idle" not in c
    assert "--slice=tisma-segundo-plano.slice" not in detonar.como_usuario(["prog"], 1000, 1000, 50)
