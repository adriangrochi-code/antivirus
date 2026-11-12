"""Teclados falsos (BadUSB): se bloquean por cómo escriben, sin molestar a las personas."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from escudo import teclados as T  # noqa: E402


class Reloj:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Disp:
    name, phys, path = "Teclado X", "usb-0000:00:14.0-2/input0", "/dev/input/event9"

    def __init__(self):
        self.agarrado = False

    def grab(self):
        self.agarrado = True

    def ungrab(self):
        self.agarrado = False


class Salida:
    def __init__(self, d):
        self.eventos, self.cerrada = [], False

    def write(self, t, c, v):
        self.eventos.append((t, c, v))

    def syn(self):
        pass

    def close(self):
        self.cerrada = True


@pytest.fixture
def ent(tmp_path):
    reloj, avisos, bloqueados, salidas = Reloj(), [], [], []
    reg = T.Registro(tmp_path / "t.json")

    def vigia(nuevo=True, ident="1234:abcd:XYZ"):
        return T.Vigia(Disp(), ident, nuevo, reg, bloqueados.append, lambda *a: avisos.append(a),
                       lambda d: salidas.append(Salida(d)) or salidas[-1], reloj=reloj)
    return {"reloj": reloj, "avisos": avisos, "bloqueados": bloqueados, "salidas": salidas, "reg": reg,
            "vigia": vigia, "ruta": tmp_path / "t.json"}


def teclear(v, reloj, n, intervalo, codigo=30, irregular=False):
    import random
    rnd = random.Random(1)
    for i in range(n):
        v.evento(1, codigo, 1)
        v.evento(1, codigo, 0)
        reloj.t += intervalo * (rnd.uniform(0.4, 1.8) if irregular else 1)


def test_inyeccion_al_conectar_se_bloquea_sin_que_llegue_una_letra(ent):
    v = ent["vigia"]()
    v.empezar()
    assert v.d.agarrado                               # nuevo: pasa por Escudo
    teclear(v, ent["reloj"], 40, 0.004)               # ~250 teclas/s, como un Rubber Ducky
    assert v.bloqueado and ent["bloqueados"] and "imposible" in ent["avisos"][0][2]
    assert ent["salidas"][0].eventos == []            # NADA llegó al escritorio
    assert "1234:abcd:XYZ" in json.loads(ent["ruta"].read_text())["bloqueados"]


def test_una_persona_escribe_normal(ent):
    v = ent["vigia"]()
    v.empezar()
    teclear(v, ent["reloj"], 30, 0.15, irregular=True)  # ~7 teclas/s, irregular
    assert not v.bloqueado
    pulsaciones = [e for e in ent["salidas"][0].eventos if e[2] == 1]
    assert len(pulsaciones) == 30                      # lo retenido en los primeros segundos también llegó


def test_lo_retenido_se_entrega_aunque_no_se_escriba_mas(ent):
    v = ent["vigia"]()
    v.empezar()
    teclear(v, ent["reloj"], 3, 0.2, irregular=True)
    assert ent["salidas"][0].eventos == []            # dentro de la gracia
    ent["reloj"].t = T.GRACIA_S + 0.1
    v.vencio_gracia()
    assert len([e for e in ent["salidas"][0].eventos if e[2] == 1]) == 3


def test_robot_lento_pero_perfectamente_regular(ent):
    ent["reg"].conocidos.add("aaaa:bbbb")
    v = ent["vigia"](nuevo=False, ident="aaaa:bbbb")
    v.empezar()
    assert not v.d.agarrado                           # conocido: directo, vigilancia pasiva
    teclear(v, ent["reloj"], 25, 0.060)               # 16 teclas/s exactas: esquiva la regla de velocidad…
    assert v.bloqueado and "regular" in ent["avisos"][0][2] and v.d.agarrado   # …pero no la del ritmo


def test_tras_la_prueba_queda_conocido_y_se_suelta(ent):
    v = ent["vigia"]()
    v.empezar()
    teclear(v, ent["reloj"], 10, 0.2, irregular=True)
    ent["reloj"].t = T.PRUEBA_S + 1
    v.evento(1, 30, 1)
    assert not v.d.agarrado and ent["salidas"][0].cerrada and "1234:abcd:XYZ" in ent["reg"].conocidos


def test_teclas_no_quedan_pegadas_al_cortar(ent):
    v = ent["vigia"]()
    v.empezar()
    ent["reloj"].t = T.GRACIA_S + 1
    v.gracia = False
    v.evento(1, 42, 1)                                # Shift apretado (llegó al escritorio)
    for _ in range(20):
        v.evento(1, 30, 1)                            # y de golpe escribe como un robot
    assert v.bloqueado and (1, 42, 0) in ent["salidas"][0].eventos


def test_era_mio_y_no_vuelve_a_molestar(ent):
    ent["reg"].permitir("1234:abcd:XYZ")
    v = ent["vigia"]()
    v.empezar()
    teclear(v, ent["reloj"], 40, 0.004)               # lector de códigos de barras
    assert not v.bloqueado and not v.d.agarrado
    with pytest.raises(ValueError):
        ent["reg"].permitir("../../etc")


def test_lo_ya_bloqueado_se_bloquea_al_reconectar(ent):
    ent["reg"].bloqueados.add("1234:abcd:XYZ")
    v = ent["vigia"]()
    v.empezar()
    assert v.bloqueado and v.d.agarrado and ent["bloqueados"]


def test_que_se_vigila():
    class D:
        def __init__(self, phys, nombre, teclas):
            self.phys, self.name, self._t = phys, nombre, teclas

        def capabilities(self):
            return {1: self._t}
    letras = [30, 48, 46, 32, 18, 19]
    assert T.es_teclado_usb(D("usb-x/input0", "Teclado", letras))
    assert not T.es_teclado_usb(D("isa0060/serio0/input0", "AT Keyboard", letras))   # el del portátil
    assert not T.es_teclado_usb(D("usb-x/input1", "Ratón", [272, 273, 274]))        # ratón puro: no escribe
    assert not T.es_teclado_usb(D("usb-x/input0", "TISMA teclado (X)", letras))      # el nuestro
    assert T.identidad(0x1234, 0xabcd, "a b/c") == "1234:abcd:abc"


def test_boton_era_mio_en_el_aviso():
    import importlib.machinery
    import importlib.util
    ROOT = Path(__file__).resolve().parents[3]
    loader = importlib.machinery.SourceFileLoader("notif", str(ROOT / "rootfs/usr/bin/escudo-notificador"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("notif", loader))
    loader.exec_module(mod)
    orden = mod.ACCIONES["permitir_teclado"][1]
    assert orden({"exe": "1234:abcd:XYZ"}) == ["pkexec", "/usr/bin/escudo", "teclado", "permitir", "1234:abcd:XYZ"]
    assert orden({"exe": "1234:abcd; reboot"}) is None
