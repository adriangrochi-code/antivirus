"""ECC «Verificar»: manifiesto, detección y corrección al releer, alerta, desactivado por defecto, inyección."""
import json

from centinela import apply, ecc


def _entorno(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, "STATE_DIR", str(tmp_path / "estado"))
    monkeypatch.setattr(ecc, "DIR", tmp_path / "estado" / "ecc")
    monkeypatch.setattr(ecc, "ALERTA", tmp_path / "run" / "ecc-alerta.json")
    monkeypatch.setattr(ecc, "BLOQUE", 4096)
    arch = tmp_path / "m.gguf"
    arch.write_bytes(bytes(range(256)) * 16 * 5)          # 5 bloques
    return str(arch)


def test_manifiesto_y_revision_limpia(tmp_path, monkeypatch):
    a = _entorno(tmp_path, monkeypatch)
    d = ecc.manifiesto(a)
    assert len(d["huellas"]) == 5
    r = ecc.revisar(a)
    assert r["revisados"] == 5 and r["errores"] == 0 and r["siguiente"] == 0


def test_corrige_al_releer_y_avisa_si_no(tmp_path, monkeypatch):
    a = _entorno(tmp_path, monkeypatch)
    datos = open(a, "rb").read()
    malas = {2: 1}                                         # el bloque 2 viene dañado una sola vez

    def leer(i):
        b = bytearray(datos[i * 4096:(i + 1) * 4096])
        if malas.get(i):
            malas[i] -= 1
            b[7] ^= 0x10
        return bytes(b)
    descartados = []
    r = ecc.revisar(a, leer=leer, descartar=descartados.append)
    assert r["errores"] == 1 and r["corregidos"] == 1 and descartados == [2] and not r["sin_corregir"]

    def siempre_mal(i):
        b = bytearray(datos[i * 4096:(i + 1) * 4096])
        if i == 4:
            b[0] ^= 1
        return bytes(b)
    r = ecc.revisar(a, leer=siempre_mal, descartar=lambda i: None)
    assert r["sin_corregir"] == [4] and r["corregidos"] == 0


def test_rafaga_desactivada_por_defecto_y_alerta(tmp_path, monkeypatch):
    a = _entorno(tmp_path, monkeypatch)
    assert ecc.nivel() == "desactivado"
    assert ecc.rafaga() == {"estado": "desactivado"}
    ecc.poner_nivel("verificar")
    monkeypatch.setattr(ecc, "modelos", lambda: [a])
    r = ecc.rafaga(mb=1, pausar=lambda: False)
    assert r["estado"] == "ok" and r["errores"] == 0
    assert apply.load_state("ecc-estado.json")["_ultima"]
    ecc._alertar(a, [3])
    assert json.loads(ecc.ALERTA.read_text())["bloques"] == [3]


def test_pausa_si_la_ia_genera(tmp_path, monkeypatch):
    a = _entorno(tmp_path, monkeypatch)
    assert ecc.revisar(a, pausar=lambda: True)["revisados"] == 0


def test_inyeccion_detecta_todo_sin_falsos():
    r = ecc.prueba_inyeccion(tam_mb=16, fallos=50)
    assert r["pct_detectado"] == 100.0 and r["falsos"] == 0 and r["corregidos"] == r["detectados"]
