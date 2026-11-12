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
    assert {"esenciales", "diario", "estudio", "creacion", "ingenieria", "juegos", "programacion"} <= set(p)
    assert "libreoffice-writer" in p["esenciales"]
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


def test_completar_installs_the_base_extras_once(tmp_path, monkeypatch):
    (tmp_path / "esenciales.list").write_text("# x\nlibreoffice-writer\n")
    marca = tmp_path / "lib/hecho"
    monkeypatch.setattr(mod, "DIR", str(tmp_path))
    monkeypatch.setattr(mod, "ESTADO", str(tmp_path / "estado.json"))
    monkeypatch.setattr(mod, "HECHO", str(marca))
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", _fake_run([], calls, rc=100))
    assert mod.main(["completar"]) == 1 and not marca.exists()          # sin red: lo reintenta el servicio
    calls.clear()
    monkeypatch.setattr(mod.subprocess, "run", _fake_run([], calls))
    assert mod.main(["completar"]) == 0 and marca.exists()
    assert any("libreoffice-writer" in c for c in calls)
    calls.clear()
    assert mod.main(["completar"]) == 0 and calls == []                 # hecho: no vuelve a tocar apt


def test_installer_downloads_base_extras_after_final_sources():
    import yaml
    s = yaml.safe_load((ROOT / "rootfs/etc/calamares/settings.conf").read_text())
    assert {"id": "tisma", "module": "shellprocess", "config": "shellprocess-tisma.conf"} in s["instances"]
    ex = next(p["exec"] for p in s["sequence"] if "exec" in p)
    assert ex.index("sources-final") < ex.index("shellprocess@tisma") < ex.index("umount")
    sp = yaml.safe_load((ROOT / "rootfs/etc/calamares/modules/shellprocess-tisma.conf").read_text())
    assert sp["script"] == ["-/usr/bin/tisma-secureboot quitar-minimal", "-/usr/bin/tisma-secureboot elegir",
                            "-/usr/bin/tisma-perfil completar"] and sp["timeout"] >= 1800   # «-»: sin red no falla
    unit = (ROOT / "rootfs/etc/systemd/system/tisma-completar.service").read_text()
    assert "ConditionPathExists=!/var/lib/tisma/completar-hecho" in unit and "!boot=live" in unit


def test_games_profile_reenables_32bit_and_asks_for_reboot(tmp_path, monkeypatch):
    (tmp_path / "juegos.list").write_text("# J\nsteam-installer\n")
    marca = tmp_path / "etc/32bits"
    monkeypatch.setattr(mod, "DIR", str(tmp_path))
    monkeypatch.setattr(mod, "ESTADO", str(tmp_path / "estado.json"))
    monkeypatch.setattr(mod, "MARCA_32", str(marca))
    calls = []
    assert mod.instalar(["juegos"], _fake_run([], calls)) == 0
    assert marca.exists() and ["update-grub"] in calls
    assert "Reinicia" in json.loads((tmp_path / "estado.json").read_text())["mensaje"]
    calls.clear()
    (tmp_path / "otro.list").write_text("# O\nalgo\n")
    assert mod.instalar(["otro"], _fake_run([], calls)) == 0 and ["update-grub"] not in calls


def test_32bit_is_off_by_default_in_grub(tmp_path):
    cfg = (ROOT / "rootfs/etc/default/grub.d/52-tisma-32bits.cfg").read_text()
    out = subprocess.run(["sh", "-c", cfg.replace("/etc/tisma/32bits", str(tmp_path / "no")) +
                          '\necho "$GRUB_CMDLINE_LINUX_DEFAULT"'], capture_output=True, text=True).stdout
    assert "ia32_emulation=0" in out and "vdso32=0" in out
    (tmp_path / "si").write_text("")
    out = subprocess.run(["sh", "-c", cfg.replace("/etc/tisma/32bits", str(tmp_path / "si")) +
                          '\necho "$GRUB_CMDLINE_LINUX_DEFAULT"'], capture_output=True, text=True).stdout
    assert "ia32_emulation" not in out


def test_usos_elegidos_al_instalar_se_suman(tmp_path, monkeypatch):
    """Página «Tu uso» del instalador: varios usos (o «Todo») y se descargan sumados con la base."""
    import sys
    import re
    import types
    import yaml
    # Módulo de Calamares con un libcalamares falso
    escritos = {}
    gs = types.SimpleNamespace(value=lambda k: {"packagechooser_perfiles": "juegos,programacion,juegos",
                                                "rootMountPoint": str(tmp_path)}.get(k))
    fake = types.SimpleNamespace(globalstorage=gs, utils=types.SimpleNamespace(debug=lambda m: escritos.update(m=m)))
    monkeypatch.setitem(sys.modules, "libcalamares", fake)
    spec = importlib.util.spec_from_file_location(
        "tismaperfiles", ROOT / "rootfs/usr/lib/calamares/modules/tismaperfiles/main.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.elegidos("todo") == list(m.PERFILES) and m.elegidos("") == [] and m.elegidos("root,diario") == ["diario"]
    assert m.run() is None
    elegido = tmp_path / "etc/tisma/perfiles-elegidos"
    assert elegido.read_text().split() == ["juegos", "programacion"]
    assert oct(elegido.stat().st_mode & 0o777) == "0o644"
    # tisma-perfil completar: la base + las listas de esos usos («juegos» trae también «creacion»)
    tp = mod
    assert tp.listas_elegidas(str(elegido)) == ["juegos", "creacion", "programacion"]
    # Mismos usos en el instalador, en tisma-perfil y en la Bienvenida, con las mismas listas
    sys.path.insert(0, str(ROOT / "userspace/bienvenida"))
    from tisma_bienvenida import server as bv
    assert set(m.PERFILES) == set(tp.LISTAS_DE_USO) == set(bv.PERFILES)
    for uso, listas in tp.LISTAS_DE_USO.items():
        assert [x for x in bv.PERFILES[uso]["listas"] if x != "esenciales"] == listas, uso
        for lista in listas:
            assert (ROOT / f"rootfs/usr/share/tisma/perfiles/{lista}.list").exists()
    # Orden del instalador: página antes del resumen; se guarda antes de descargar
    st = yaml.safe_load((ROOT / "rootfs/etc/calamares/settings.conf").read_text())
    show, exe = st["sequence"][0]["show"], st["sequence"][1]["exec"]
    assert show.index("packagechooserq@perfiles") < show.index("summary")
    assert exe.index("tismaperfiles") < exe.index("shellprocess@tisma")
    pc = yaml.safe_load((ROOT / "rootfs/etc/calamares/modules/packagechooserq-perfiles.conf").read_text())
    assert pc["method"] == "legacy"   # guarda «a,b» tal cual en packagechooser_perfiles (lo que lee tismaperfiles)
    qml = ROOT / ("rootfs" + pc["qmlFilename"])
    assert qml.exists()
    # Las tarjetas de la página son exactamente los usos (casillas, un clic marca o desmarca)
    assert set(re.findall(r'id: "(\w+)", nombre:', qml.read_text())) == set(m.PERFILES)
