"""Mantenimiento: espera a equipo libre, no salta la tarea, y no espera para siempre."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from centinela import mantenimiento as mt, sysfs  # noqa: E402


class Reloj:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def correr(estados, energia=(True, None), max_s=3600, abs_s=7200):
    r = Reloj()
    seq = iter(estados)
    ultimo = [(False, "?")]

    def libre_f():
        try:
            ultimo[0] = next(seq)
        except StopIteration:
            pass
        return ultimo[0]
    rc = mt.esperar(max_s=max_s, abs_s=abs_s, paso=60, libre_f=libre_f, energia_f=lambda: energia,
                    now=r.now, sleep=r.sleep, log=lambda *_: None)
    return rc, r.t


def test_runs_once_free_twice_in_a_row():
    rc, t = correr([(False, "equipo en uso"), (True, "libre"), (False, "equipo en uso"), (True, "libre"),
                    (True, "libre")])
    assert rc == 0 and t < 600


def test_never_free_on_ac_runs_anyway_after_max():
    rc, t = correr([(False, "equipo en uso")], energia=(True, None), max_s=3600)
    assert rc == 0 and 3600 <= t < 3700


def test_low_battery_never_forced_and_gives_up_at_absolute_limit():
    rc, t = correr([(False, "a batería")], energia=(False, 12), max_s=3600, abs_s=7200)
    assert rc == 1 and 7200 <= t < 7300


def test_good_battery_is_forced_after_max():
    rc, _ = correr([(False, "a batería")], energia=(False, 80), max_s=3600)
    assert rc == 0


def test_libre_reasons():
    assert mt.libre(lambda: (False, 50), lambda: True, lambda: False) == (False, "a batería")
    assert mt.libre(lambda: (True, None), lambda: True, lambda: True) == (False, "la IA local está generando")
    assert mt.libre(lambda: (True, None), lambda: False, lambda: False) == (False, "equipo en uso")
    assert mt.libre(lambda: (True, None), lambda: True, lambda: False) == (True, "libre")


def test_power_supply_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(sysfs, "ROOT", str(tmp_path))
    assert mt.energia() == (True, None)                            # sobremesa sin batería: «enchufado»
    ps = tmp_path / "sys/class/power_supply"
    (ps / "AC").mkdir(parents=True)
    (ps / "AC/type").write_text("Mains\n")
    (ps / "AC/online").write_text("0\n")
    (ps / "BAT0").mkdir()
    (ps / "BAT0/type").write_text("Battery\n")
    (ps / "BAT0/capacity").write_text("41\n")
    assert mt.energia() == (False, 41)
    (ps / "AC/online").write_text("1\n")
    assert mt.energia() == (True, 41)
