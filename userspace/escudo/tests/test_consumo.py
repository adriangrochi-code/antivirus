"""Mineros y abuso de CPU: avisos solo con señales claras; compilar o jugar no cuenta."""
from escudo import consumo
from escudo.consumo import VigiaConsumo, sospechoso

T = consumo.TICKS


def correr(v, exe, nombre="miner", cpu=1.0, minutos=12, paso=30, pid=4242):
    alertas, ticks = [], 0
    for i in range(int(minutos * 60 / paso) + 1):
        alertas += v.paso({pid: (ticks, exe, nombre)}, i * paso)
        ticks += int(cpu * T * paso)
    return alertas


def test_quien_cuenta():
    assert sospechoso("/home/ana/Descargas/juego")
    assert sospechoso("/tmp/.x/xmr")
    assert not sospechoso("/usr/bin/python3")
    assert not sospechoso("/home/ana/.rustup/toolchains/stable/bin/rustc")
    assert not sospechoso("/home/ana/.local/share/Steam/steamapps/common/Juego/juego")


def test_conexion_a_mineria_aviso_grave_una_vez():
    v = VigiaConsumo(conexion=lambda pid: "1.2.3.4:3333", bloqueada=lambda: False)
    a = correr(v, "/home/ana/Descargas/x", minutos=5)
    assert len(a) == 1 and a[0].severity == "high" and "minería" in a[0].detail and a[0].boton == "ver_informe"


def test_nombre_disfrazado():
    v = VigiaConsumo(conexion=lambda pid: None, bloqueada=lambda: False)
    a = correr(v, "/dev/shm/.k", nombre="kworker/0:1", minutos=3)
    assert len(a) == 1 and "kworker" in a[0].detail


def test_pantalla_bloqueada_diez_minutos():
    v = VigiaConsumo(conexion=lambda pid: None, bloqueada=lambda: True)
    assert not correr(v, "/home/ana/app", minutos=8)
    v = VigiaConsumo(conexion=lambda pid: None, bloqueada=lambda: True)
    a = correr(v, "/home/ana/app", minutos=12)
    assert len(a) == 1 and a[0].severity == "medium"


def test_usuario_presente_o_poca_cpu_no_avisa():
    assert not correr(VigiaConsumo(conexion=lambda p: None, bloqueada=lambda: False), "/home/ana/app", minutos=30)
    assert not correr(VigiaConsumo(conexion=lambda p: "1.2.3.4:3333", bloqueada=lambda: True), "/home/ana/app",
                      cpu=0.2, minutos=30)


def test_muestra_real_no_falla():
    assert isinstance(consumo.muestra(), dict)
    assert consumo.conexion_minera(1) in (None,) or isinstance(consumo.conexion_minera(1), str)
