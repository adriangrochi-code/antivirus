"""tisma-perfil: apps por perfil que no van en la ISO (paquetes de Debian y componentes de GitHub)."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ["TISMA_TEST"] = "1"
loader = importlib.machinery.SourceFileLoader("perfil", str(ROOT / "rootfs/usr/bin/tisma-perfil"))
spec = importlib.util.spec_from_loader("perfil", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
LISTAS = ROOT / "rootfs/usr/share/tisma/perfiles"


def test_real_profiles_parse_and_only_hold_valid_names():
    p = mod.perfiles(str(LISTAS))
    assert {"diario", "estudio", "creacion", "ingenieria", "juegos", "programacion"} <= set(p)
    assert "steam-installer" in p["juegos"] and "mangohud:i386" in p["juegos"]
    assert "@craft-anything" in p["ingenieria"] and "@rea" in p["programacion"]
    for pk in p.values():
        assert pk and all(" " not in x and not x.startswith("-") for x in pk)
    for c in {x[1:] for pk in p.values() for x in pk if x.startswith("@")}:
        assert (ROOT / "iso/components" / f"{c}.sh").is_file()


def test_profile_packages_are_not_duplicated_in_the_base_iso():
    base = set()
    for line in (ROOT / "iso/packages.list").read_text().splitlines():
        base |= set(line.split("#")[0].split())
    for nombre, pk in mod.perfiles(str(LISTAS)).items():
        assert not base & set(pk), f"{nombre}: {base & set(pk)} ya está en la base"


def test_hostile_names_never_reach_apt(tmp_path):
    (tmp_path / "x.list").write_text("ok-pkg -o=APT::Foo --allow-unauthenticated ../../etc @../malo @bien\n")
    assert mod.perfiles(str(tmp_path)) == {"x": ["ok-pkg", "@bien"]}


def _fake_run(installed, calls, rc=0):
    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "dpkg-query":
            out = "".join(f"{n} ii \n" for n in installed)
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, rc, "", "fallo de red" if rc else "")
    return run


def test_missing_packages_respect_architecture():
    calls = []
    run = _fake_run(["wine32:i386", "mangohud:amd64"], calls)
    assert mod.faltan(["wine", "wine32:i386", "mangohud", "mangohud:i386"], run) == ["wine", "mangohud:i386"]


def test_install_flow_writes_progress_and_runs_components(tmp_path, monkeypatch):
    (tmp_path / "p.list").write_text("# Prueba\nsteam-installer libc6 @comp\n")
    comps = tmp_path / "comp"
    comps.mkdir()
    (comps / "comp.sh").write_text("#!/bin/sh\n")
    (comps / "comp.sh").chmod(0o755)
    monkeypatch.setattr(mod, "DIR", str(tmp_path))
    monkeypatch.setattr(mod, "COMPONENTES", str(comps))
    monkeypatch.setattr(mod, "ESTADO", str(tmp_path / "run/estado.json"))
    calls = []
    assert mod.instalar(["p"], _fake_run(["libc6:amd64"], calls)) == 0
    assert any(c[0] == "debconf-set-selections" for c in calls)                 # licencia de Steam preaceptada
    inst = [c for c in calls if "install" in c]
    assert inst and inst[0][-1] == "steam-installer" and "libc6" not in inst[0]  # solo lo que falta
    assert [str(comps / "comp.sh"), "/"] in calls
    st = json.loads((tmp_path / "run/estado.json").read_text())
    assert st["ok"] is True and st["fase"] == "listo" and st["paquetes"] == 1


def test_unknown_profile_and_apt_failure(tmp_path, monkeypatch):
    (tmp_path / "p.list").write_text("algo\n")
    monkeypatch.setattr(mod, "DIR", str(tmp_path))
    monkeypatch.setattr(mod, "ESTADO", str(tmp_path / "estado.json"))
    assert mod.instalar(["nope"], _fake_run([], [])) == 2
    assert mod.instalar([], _fake_run([], [])) == 2
    assert mod.instalar(["p"], _fake_run([], [], rc=100)) == 1
    st = json.loads((tmp_path / "estado.json").read_text())
    assert st["ok"] is False and "fallo de red" in st["mensaje"]
