"""Modo IA: arriendo, pausa del sistema, efectos de KWin y Baloo, y que todo vuelve como estaba."""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from centinela import comparar as cp, modo_ia as mi, sysfs  # noqa: E402


class Escritorio:
    """KWin y Baloo simulados: registra las órdenes y responde como los de verdad."""
    def __init__(self, cargados=("blur", "contrast"), baloo="Baloo File Indexer is running"):
        self.cargados, self.baloo, self.ordenes = set(cargados), baloo, []

    def __call__(self, argv, **kw):
        self.ordenes.append(argv)
        out = ""
        if argv[0] == "gdbus":
            metodo, efecto = argv[-2].rsplit(".", 1)[-1], argv[-1]
            if metodo == "isEffectLoaded":
                out = "(true,)" if efecto in self.cargados else "(false,)"
            elif metodo == "unloadEffect":
                self.cargados.discard(efecto)
            elif metodo == "loadEffect":
                self.cargados.add(efecto)
        elif argv[:2] == ["balooctl6", "status"]:
            out = self.baloo
        elif argv[:2] == ["balooctl6", "suspend"]:
            self.baloo = "Baloo File Indexer is suspended"
        elif argv[:2] == ["balooctl6", "resume"]:
            self.baloo = "Baloo File Indexer is running"
        return subprocess.CompletedProcess(argv, 0, out, "")


def test_session_only_changes_and_restores_what_was_on():
    e = Escritorio(cargados=("blur",), baloo="Baloo File Indexer is suspended")   # Baloo ya en pausa: no se toca
    cambios = mi.sesion_entrar(e)
    assert cambios == {"efectos": ["blur"], "baloo": False} and e.cargados == set()
    mi.sesion_salir(cambios, e)
    assert e.cargados == {"blur"} and not any(o[:2] == ["balooctl6", "resume"] for o in e.ordenes)


def test_no_kwin_or_baloo_is_harmless():
    def sin_nada(argv, **kw):
        raise FileNotFoundError(argv[0])
    assert mi.sesion_entrar(sin_nada) == {"efectos": [], "baloo": False}
    mi.sesion_salir({"efectos": ["blur"], "baloo": True}, sin_nada)


def test_generando_is_reentrant_holds_between_steps_and_restores(tmp_path):
    e = Escritorio()
    g = mi.Generando(run=e, estado=str(tmp_path / "estado.json"), retencion=0.15, renueva=0.05)
    g.path = str(tmp_path / "arriendo")
    with g:
        with g:                                                      # llamadas anidadas: una sola entrada
            assert e.cargados == set() and e.baloo.endswith("suspended")
            assert Path(g.path).exists() and Path(g.estado).exists()
    assert e.cargados == set()                                       # retención: el siguiente paso llega enseguida
    with g:
        pass
    n_unload = sum(1 for o in e.ordenes if o[-2].endswith("unloadEffect")) if e.ordenes else 0
    assert n_unload == 2                                             # no se apagó y encendió entre pasos
    time.sleep(0.4)
    assert e.cargados == {"blur", "contrast"} and e.baloo.endswith("running")
    assert not Path(g.path).exists() and not Path(g.estado).exists()


def test_cerrar_restores_immediately_and_recuperar_after_crash(tmp_path):
    e = Escritorio()
    g = mi.Generando(run=e, estado=str(tmp_path / "estado.json"), retencion=60)
    g.path = str(tmp_path / "arriendo")
    with g:
        pass
    g.cerrar()
    assert e.cargados == {"blur", "contrast"}
    # «Cuelgue»: entró y nunca salió; el siguiente arranque de TISMA Code lo arregla
    e2 = Escritorio()
    g2 = mi.Generando(run=e2, estado=str(tmp_path / "e2.json"), retencion=60)
    g2.path = str(tmp_path / "a2")
    g2.__enter__()
    assert e2.cargados == set()
    nuevo = mi.Generando(run=e2, estado=str(tmp_path / "e2.json"))
    assert nuevo.recuperar() is True and e2.cargados == {"blur", "contrast"}
    assert nuevo.recuperar() is False                                # ya no queda nada que recuperar


def test_lease_freshness_and_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(sysfs, "ROOT", str(tmp_path))
    d = tmp_path / "run/user/1000"
    d.mkdir(parents=True)
    f = d / mi.NOMBRE
    assert mi.activo() is False
    f.write_text("")
    assert mi.activo() is True
    old = time.time() - 120
    os.utime(f, (old, old))
    assert mi.activo() is False                                      # TISMA Code se cerró o se colgó: caduca


def test_system_pauses_ksm_only_if_it_was_running():
    estado = {"/sys/kernel/mm/ksm/run": "1"}
    s = mi.Sistema(read=lambda p: estado[p], write=lambda p, v: estado.__setitem__(p, v))
    s.pausar()
    assert estado["/sys/kernel/mm/ksm/run"] == "0"
    s.pausar()                                                       # idempotente
    s.reanudar()
    assert estado["/sys/kernel/mm/ksm/run"] == "1"
    estado["/sys/kernel/mm/ksm/run"] = "0"                           # KSM apagado por el usuario: no se enciende
    s.pausar()
    s.reanudar()
    assert estado["/sys/kernel/mm/ksm/run"] == "0"


def test_modo_ia_adjustment_is_measured_but_never_left_on(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "ESTADO", str(tmp_path))
    aj = cp.AJUSTES["modo-ia"]
    assert aj.persistente is False
    estado = {"on": False}
    falso = cp.Ajuste("modo-ia", "x", lambda: {}, lambda: estado.update(on=True),
                      lambda s: estado.update(on=False), pruebas=["p"], persistente=False)
    n = iter(range(100))
    r = cp.ejecutar(falso, rondas=4, dejar=True, progreso=lambda *_: None, pausa=0,
                    pruebas={"p": lambda c: {"ia.generacion_ts": 10 + (5 if estado["on"] else 0) + next(n) * 0.01}})
    assert r["veredicto"] == "mejor" and r["aplicado"] is False and estado["on"] is False
    assert cp.veredicto_guardado("modo-ia") == "mejor"
    assert oct(os.stat(tmp_path / "decisiones.json").st_mode & 0o777) == "0o644"
