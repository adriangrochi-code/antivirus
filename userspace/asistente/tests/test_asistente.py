import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_asistente import rutina  # noqa: E402
from tisma_asistente.clap import ClapConfig, ClapDetector  # noqa: E402

BLOCK = 0.04


def run(events, seconds=4.0, noise=0.002, seed=1):
    """events: {instante: nivel}. Devuelve los instantes en que se detectó doble palmada."""
    rnd = random.Random(seed)
    det, fired, t = ClapDetector(ClapConfig()), [], 0.0
    while t < seconds:
        level = events.get(round(t, 2), noise * (0.7 + 0.6 * rnd.random()))
        if det.feed(level, t):
            fired.append(round(t, 2))
        t = round(t + BLOCK, 2)
    return fired


def test_double_clap_fires_once():
    assert run({1.0: 0.3, 1.24: 0.25}) == [1.24]


def test_single_clap_or_far_apart_does_not_fire():
    assert run({1.0: 0.3}) == []
    assert run({1.0: 0.3, 2.0: 0.3}) == []


def test_long_loud_sound_is_one_spike():
    # un golpe largo (4 bloques seguidos) no cuenta como dos palmadas
    assert run({1.0: 0.3, 1.04: 0.3, 1.08: 0.3, 1.12: 0.3}) == []


def test_cooldown_after_fire():
    assert run({1.0: 0.3, 1.2: 0.3, 1.4: 0.3, 1.6: 0.3}) == [1.2]


def test_speech_raises_floor_less_than_claps():
    # conversación moderada: muchos niveles medios no deben disparar
    talk = {round(0.5 + i * BLOCK, 2): 0.008 + 0.004 * (i % 3) for i in range(60)}
    assert run(talk) == []


def test_routine_dry_run_and_validation(tmp_path, monkeypatch):
    cfg = tmp_path / "rutina.json"
    cfg.write_text(json.dumps({"saludo": "{saludo}. {estado}", "acciones": [
        {"tipo": "perfil", "valor": "rendimiento"}, {"tipo": "app", "valor": "net.tisma.Code"},
        {"tipo": "comando", "valor": "rm -rf /"}, {"tipo": "perfil", "valor": "turbo"}]}))
    monkeypatch.setattr(rutina, "CONFIG", cfg)
    out = rutina.run(dry_run=True)
    assert out[0].startswith("decir: Buen")
    assert "centinela-perfil@rendimiento.service" in out[1]
    assert "net.tisma.Code" in out[2]
    assert out[3].startswith("error: comando") and out[4].startswith("error: perfil")
