"""Calor: el procesador frenado por temperatura se cuenta solo con trabajo, y se resume por semana."""
import json

from centinela import calor


def test_frenado_solo_caliente_lento_y_ocupado():
    assert calor.frenado(92, 0.5, 0.9)
    assert not calor.frenado(92, 0.9, 0.9)      # caliente pero a toda velocidad
    assert not calor.frenado(70, 0.5, 0.9)      # lento pero frío (ahorro)
    assert not calor.frenado(92, 0.5, 0.2)      # sin trabajo
    assert not calor.frenado(None, 0.5, 0.9)


def test_acumula_por_dia_y_guarda(tmp_path):
    t = [1_700_000_000.0]
    c = calor.Calor(str(tmp_path / "c.json"), reloj=lambda: t[0])
    cpu = [0, 0]
    for _ in range(220):                          # 11 min con la CPU llena y frenada
        t[0] += 3
        cpu[0] += 300
        cpu[1] += 300
        c.tick(temp=93, frec=0.45, cpu=tuple(cpu))
    c.guardar()
    dias = json.load(open(tmp_path / "c.json"))
    r = calor.resumen(dias)
    assert r["frenado_min"] >= 9 and r["proporcion"] > 0.9 and r["max_c"] == 93
    assert calor.se_frena_seguido(r)


def test_inactivo_no_cuenta(tmp_path):
    t = [1_700_000_000.0]
    c = calor.Calor(str(tmp_path / "c.json"), reloj=lambda: t[0])
    for i in range(50):
        t[0] += 3
        c.tick(temp=93, frec=0.4, cpu=(i * 10, i * 300))
    assert calor.resumen(c.dias)["ocupado_min"] == 0
