"""tisma-adapt: histéresis, solo rutas que existen, un ajuste nuevo a la vez con prueba y reversión, techo
térmico y arranques fallidos."""
import json
from pathlib import Path

import pytest

from centinela import adapt, apply, sysfs

PERFIL = json.loads((Path(__file__).resolve().parents[3] / "docs/perfiles/thinkpad-a275.hipotesis.json").read_text())


@pytest.fixture
def equipo(tmp_path, monkeypatch):
    raiz = tmp_path / "raiz"
    def f(rel, v):
        p = raiz / rel.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(v)
    for n, sib in ((0, "0-1"), (1, "0-1"), (2, "2-3"), (3, "2-3")):
        f(f"/sys/devices/system/cpu/cpu{n}/cpufreq/scaling_governor", "schedutil")
        f(f"/sys/devices/system/cpu/cpu{n}/topology/thread_siblings_list", sib)
        f(f"/sys/devices/system/cpu/cpu{n}/online", "1")
    f("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors", "conservative ondemand schedutil performance")
    f("/sys/devices/system/cpu/cpufreq/boost", "1")
    f("/sys/module/pcie_aspm/parameters/policy", "[default] performance powersave powersupersave")
    f("/sys/class/drm/card0/device/power_dpm_force_performance_level", "auto")
    monkeypatch.setattr(sysfs, "ROOT", str(raiz))
    for nombre in ("NEGRA", "VALIDADOS", "ARRANQUES", "POLITICA"):
        monkeypatch.setattr(adapt, nombre, tmp_path / f"{nombre}.json")
    return raiz


def test_histeresis():
    t = [0.0]
    c = adapt.Clasificador(sostener_s=15, reloj=lambda: t[0])
    assert c.paso("lote") == "interactivo"
    t[0] = 10
    assert c.paso("lote") == "interactivo"
    t[0] = 16
    assert c.paso("lote") == "lote"
    t[0] = 17
    assert c.paso("inactivo") == "lote"            # un parpadeo no cambia la política
    assert adapt.Clasificador.candidata(0.9, set(), False, {"cc1plus"}) == "lote"
    assert adapt.Clasificador.candidata(0.1, {"juego"}, False, set()) == "juego"
    assert adapt.Clasificador.candidata(0.9, set(), True, {"cc1"}) == "ia"       # lo declarado manda


def test_escrituras_solo_lo_que_existe(equipo):
    assert adapt.escrituras("cpu.governor", "conservative")[0][1] == "conservative"
    assert adapt.escrituras("cpu.governor", "userspace") == []                    # no disponible en el equipo
    assert adapt.escrituras("sata.alpm", "min_power") == []                       # este equipo no tiene SATA
    assert adapt.escrituras("cpu.modulos_activos", 1) == [("/sys/devices/system/cpu/cpu2/online", "0"),
                                                            ("/sys/devices/system/cpu/cpu3/online", "0")]
    for p, _ in adapt.escrituras("gpu.dpm", "low") + adapt.escrituras("cpu.boost", 0):
        assert apply.Actuator.allowed(p)


def test_un_ajuste_nuevo_a_la_vez_y_reversion(equipo):
    t = [0.0]
    log = {"txt": ""}
    a = adapt.Adaptador(PERFIL, apply.Actuator(), reloj=lambda: t[0], prueba_s=120,
                        errores_kernel=lambda s: log["txt"], temperatura=lambda: 50)
    aspm = equipo / "sys/module/pcie_aspm/parameters/policy"
    pol = None
    for i in range(60):                                     # equipo quieto: pasa a «inactivo» y prueba de a uno
        t[0] = i * 20
        pol = a.tick("eco", False, 0.01, set(), False, set())
        assert len(pol["en_prueba"]) <= 1
        if "pcie.aspm=powersupersave" in pol["en_prueba"]:
            break
    assert pol["clase"] == "inactivo" and aspm.read_text() == "powersupersave"
    log["txt"] = "pcieport 0000:00:02.1: AER: Corrected error received"     # el ajuste da errores
    t[0] += 20
    pol = a.tick("eco", False, 0.01, set(), False, set())
    assert "pcie.aspm=powersupersave" in json.loads(adapt.NEGRA.read_text())["ajustes"]
    assert aspm.read_text() == "default"                                    # revertido a la opción anterior
    assert "pcie.aspm" not in pol["ajustes"]                                # y no se vuelve a usar
    validados = json.loads(adapt.VALIDADOS.read_text())["ajustes"]
    assert "cpu.boost=0" in validados                                       # los demás quedaron validados


def test_maximo_baja_a_normal_si_hace_calor(equipo):
    t = [0.0]
    temp = {"c": 95}
    a = adapt.Adaptador(PERFIL, apply.Actuator(dry_run=True), reloj=lambda: t[0], temperatura=lambda: temp["c"])
    assert a.modo("maximo") == "maximo"
    t[0] = 61
    assert a.modo("maximo") == "normal"
    temp["c"] = 85                                                 # todavía cerca del techo: sigue en Normal
    assert a.modo("maximo") == "normal"
    temp["c"] = 70
    assert a.modo("maximo") == "maximo"


def test_dos_arranques_fallidos_suspenden_el_perfil(equipo):
    assert adapt.arranque_inicio() == 1
    adapt.arranque_ok()
    assert adapt.arranque_inicio() == 1
    assert adapt.arranque_inicio() == 2                            # el anterior no llegó a 3 minutos
    assert adapt.perfil_activo() is None
    adapt.reanudar_perfil()
    assert not json.loads(adapt.ARRANQUES.read_text()).get("suspendido")


def test_perfil_suspendido_avisa_una_vez_con_boton(equipo, tmp_path):
    import json
    al = tmp_path / "escudo" / "alertas.jsonl"
    assert adapt.avisar_suspendido(str(al)) is False                 # nada suspendido: sin aviso
    adapt.arranque_inicio()
    adapt.arranque_inicio()                                          # suspendido
    assert adapt.avisar_suspendido(str(al)) is True
    a = json.loads(al.read_text())
    assert a["boton"] == "reanudar_perfil" and a["kind"] == "sistema"
    assert adapt.avisar_suspendido(str(al)) is False                 # una sola vez
    adapt.reanudar_perfil()
    assert not adapt._leer_json(adapt.ARRANQUES).get("suspendido")
    src = (Path(__file__).resolve().parents[3] / "rootfs/usr/bin/escudo-notificador").read_text()
    assert '"reanudar_perfil": ("Volver a usarlos", lambda a: ["pkexec", "/usr/bin/centinela", "adapt", "reanudar"]' in src
