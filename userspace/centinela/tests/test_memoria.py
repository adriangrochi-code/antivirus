"""Compactación de apps en segundo plano: solo las inactivas, nunca las protegidas, con límites."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from centinela import memoria, sysfs  # noqa: E402

BASE = "sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice"


def make(root, name, usage, anon):
    d = root / BASE / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "cpu.stat").write_text(f"usage_usec {usage}\nuser_usec 1\n")
    (d / "memory.stat").write_text(f"anon {anon}\nfile 1000\n")
    return d


def test_only_idle_unprotected_apps_with_ram_pressure(tmp_path, monkeypatch):
    monkeypatch.setattr(sysfs, "ROOT", str(tmp_path))
    t = [0.0]
    writes = []
    c = memoria.Compactor(now=lambda: t[0], write=lambda p, v: writes.append((p.rsplit("/", 2)[-2], int(v))) or True)
    make(tmp_path, "app-org.kde.okular@1.scope", 100, 800 << 20)        # inactiva, 800 MB
    make(tmp_path, "app-firefox@2.scope", 100, 3 << 30)                  # inactiva, 3 GB → tope 1 GB
    make(tmp_path, "app-org.kde.plasmashell.service", 100, 500 << 20)   # protegida
    make(tmp_path, "app-tisma-code@3.scope", 100, 900 << 20)            # el motor de IA: protegida
    make(tmp_path, "app-kate@4.scope", 100, 10 << 20)                   # demasiado pequeña
    active = make(tmp_path, "app-krita@5.scope", 100, 900 << 20)
    c.step(4000, 16000)                                   # primera pasada: se toma nota
    t[0] = 700
    (active / "cpu.stat").write_text("usage_usec 999\n")  # Krita sigue trabajando
    assert c.step(12000, 16000) == []                     # sobra RAM: no se hace nada
    t[0] = 760
    (active / "cpu.stat").write_text("usage_usec 1999\n")
    done = dict(c.step(4000, 16000))
    assert done == {"app-org.kde.okular@1.scope": 400 << 20, "app-firefox@2.scope": 1 << 30}
    t[0] = 900
    assert c.step(4000, 16000) == []                      # enfriamiento de 15 min por app
