"""Emergencias: TISMA revisa SOLO en riesgo grave; contenido sellado, aviso sin contenido, acceso auditado."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_ia_escolar import datos as dmod, emergencias as em, servidor  # noqa: E402


class Modelo:
    def __call__(self, endpoint, modelo, mensajes, max_tokens):
        return "Gracias por contarme. Lo que sentís importa.", 10


@pytest.fixture
def todo(tmp_path):
    d = dmod.Datos(tmp_path / "d.db", b"p" * 32)
    inst = d.crear_institucion("Escuela 7")
    g = d.crear_grupo(inst, "5.º B", "primaria")
    _, ficha = d.vincular(d.crear_codigo(g), alias="Mateo, n.º 3")
    priv, pub = em.generar_claves()
    avisos = []
    e = em.Emergencias(d, pub, webhook="https://alertas.tisma/x", enviar=avisos.append)
    app = servidor.App(d, "http://m/v1", "qwen", llamar=Modelo(), emergencias=e)
    return {"d": d, "cuenta": d.cuenta(ficha), "app": app, "priv": priv, "avisos": avisos, "db": tmp_path / "d.db"}


def preguntar(t, texto, historia=()):
    msgs = [*historia, {"role": "user", "content": texto}]
    return t["app"].responder(t["cuenta"], msgs)


def esperar(avisos, n=1):
    import time
    for _ in range(100):
        if len(avisos) >= n:
            return
        time.sleep(0.01)


def test_solo_riesgo_grave_genera_emergencia(todo):
    preguntar(todo, "¿qué es una fracción?")
    preguntar(todo, "me hacen bullying en la escuela")            # acoso: alerta al docente, no emergencia
    assert em.Consola(todo["d"], "Ana Pérez").pendientes() == []
    preguntar(todo, "un adulto me pidió fotos y me dijo que no le diga a nadie")
    p = em.Consola(todo["d"], "Ana Pérez").pendientes()
    assert len(p) == 1 and p[0]["categoria"] == "abuso" and p[0]["institucion"] == "Escuela 7"


def test_contenido_sellado_ilegible_sin_la_clave_privada(todo):
    preguntar(todo, "estoy triste", )
    preguntar(todo, "quiero hacerme daño esta noche", [{"role": "user", "content": "nadie me quiere en casa"},
                                                        {"role": "assistant", "content": "Contame más"}])
    volcado = "\n".join(sqlite3.connect(todo["db"]).iterdump())
    for secreto in ("hacerme", "nadie me quiere", "Lo que sentís"):
        assert secreto not in volcado                        # la conversación nunca en claro (el alias sí, en cuentas)
    c = em.Consola(todo["d"], "Ana Pérez")
    eid = c.pendientes()[0]["id"]
    contenido = c.ver(eid, todo["priv"])
    assert contenido["alias"] == "Mateo, n.º 3"
    assert [m["content"] for m in contenido["mensajes"]] == ["nadie me quiere en casa", "Contame más",
                                                             "quiero hacerme daño esta noche"]
    otra_priv, _ = em.generar_claves()
    with pytest.raises(Exception):
        c.ver(eid, otra_priv)                                 # con otra clave no se abre


def test_aviso_al_equipo_sin_contenido(todo):
    preguntar(todo, "me quiero morir")
    esperar(todo["avisos"])
    aviso = todo["avisos"][0]
    assert set(aviso) == {"emergencia", "categoria", "institucion", "ts"}
    assert "morir" not in json.dumps(aviso) and "Mateo" not in json.dumps(aviso)


def test_cada_acceso_queda_auditado_y_resolver_borra_el_contenido(todo):
    preguntar(todo, "me quiero morir")
    c = em.Consola(todo["d"], "Ana Pérez")
    eid = c.pendientes()[0]["id"]
    c.ver(eid, todo["priv"])
    c.resolver(eid, "autoridades", "Se llamó al 911 y a la dirección")
    assert [a[1:] for a in c.auditoria(eid)] == [("Ana Pérez", "ver"), ("Ana Pérez", "resolver:autoridades")]
    with pytest.raises(KeyError):
        c.ver(eid, todo["priv"])                              # el contenido ya no existe
    assert [a[2] for a in c.auditoria(eid)][-1] == "ver"      # el intento también queda registrado
    with pytest.raises(ValueError):
        em.Consola(todo["d"], "  ")                           # anónimo no
    with pytest.raises(ValueError):
        c.resolver(eid, "borrar_todo")


def test_vencidas_se_borran(todo, monkeypatch):
    preguntar(todo, "me quiero morir")
    monkeypatch.setattr(em, "VIGENCIA_DIAS", -1)
    em.Emergencias(todo["d"], em.generar_claves()[1]).purgar()
    c = em.Consola(todo["d"], "Ana")
    with pytest.raises(KeyError):
        c.ver(c.pendientes()[0]["id"], todo["priv"])


def test_el_sello_no_se_puede_pasar_a_otra_emergencia(todo):
    preguntar(todo, "me quiero morir")
    preguntar(todo, "un adulto me pidió fotos")
    db = todo["d"].db
    s1 = db.execute("SELECT sello FROM emergencias WHERE id = 1").fetchone()[0]
    db.execute("UPDATE emergencias SET sello = ? WHERE id = 2", (s1,))
    with pytest.raises(Exception):
        em.Consola(todo["d"], "Ana").ver(2, todo["priv"])


def test_el_estudiante_sabe_que_existe(todo):
    assert "peligro" in servidor.AVISO_ESTUDIANTE and "no se guarda" in servidor.AVISO_ESTUDIANTE
