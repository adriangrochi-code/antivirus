import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_sandbox import profile as prof  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


def test_shipped_profiles_are_valid():
    files = list((ROOT / "rootfs/usr/share/tisma-sandbox/perfiles").glob("*.json"))
    assert len(files) >= 8
    for f in files:
        prof.Profile.from_dict(json.loads(f.read_text()))


@pytest.mark.parametrize("bad", [["~/.ssh"], ["~/.ssh/id_rsa"], ["~"], ["~/.config/autostart"], ["/etc"],
                                 ["~/Documentos/../.ssh"], ["~/.bashrc"]])
def test_forbidden_paths_rejected(bad):
    with pytest.raises(prof.ProfileError):
        prof.Profile.from_dict({"nombre": "x", "exec": "/usr/bin/true", "rw": bad})


@pytest.mark.parametrize("field,val", [("dbus_talk", ["*"]), ("dbus_talk", ["org.freedesktop.*"]),
                                       ("env", {"LD_PRELOAD": "/x.so"}), ("exec", "relativo"), ("nombre", "../x")])
def test_dangerous_settings_rejected(field, val):
    d = {"nombre": "x", "exec": "/usr/bin/true", field: val}
    with pytest.raises(prof.ProfileError):
        prof.Profile.from_dict(d)


def test_args_offline_by_default(tmp_path):
    p = prof.Profile.from_dict({"nombre": "x", "exec": "/usr/bin/true"})
    a = prof.bwrap_args(p, uid=1000, runtime_dir="/run/user/1000", app_home=tmp_path, dbus_socket=None,
                        exists=lambda _: True)
    assert "--unshare-net" in a and "--cap-drop" in a and "--new-session" in a
    assert "/dev/dri" not in a and str(tmp_path) in a


@pytest.mark.skipif(not shutil.which("bwrap"), reason="sin bubblewrap")
def test_real_isolation(tmp_path, monkeypatch):
    secret = Path.home() / ".tisma_sb_secret"
    secret.write_text("SECRETO")
    (Path.home() / "Documentos").mkdir(exist_ok=True)
    try:
        p = prof.Profile.from_dict({"nombre": "prueba", "exec": "/bin/sh", "rw": ["~/Documentos"]})
        args = prof.bwrap_args(p, uid=os.getuid(), runtime_dir=f"/run/user/{os.getuid()}", app_home=tmp_path,
                               dbus_socket=None)
        script = (f"cat {secret} 2>/dev/null || echo SIN_ACCESO; "
                  "python3 -c \"import socket;socket.create_connection(('1.1.1.1',53),2)\" 2>/dev/null && echo RED || echo SIN_RED; "
                  "touch /usr/x 2>/dev/null && echo ESCRIBE_SISTEMA || echo SISTEMA_RO; "
                  f"echo hola > {Path.home()}/Documentos/sb.txt && echo DOCS_OK")
        out = subprocess.run(["bwrap", *args, "--", "/bin/sh", "-c", script], capture_output=True, text=True,
                             timeout=30).stdout
        assert "SECRETO" not in out and "SIN_ACCESO" in out
        assert "SIN_RED" in out and "SISTEMA_RO" in out and "DOCS_OK" in out
        assert (Path.home() / "Documentos/sb.txt").read_text().strip() == "hola"
    finally:
        secret.unlink()
        (Path.home() / "Documentos/sb.txt").unlink(missing_ok=True)


def test_parent_of_forbidden_and_symlinked_paths_rejected(tmp_path, monkeypatch):
    base = {"nombre": "x", "exec": "/usr/bin/true"}
    for parent in (["~/.config"], ["~/.local"], ["~/.local/share"]):
        with pytest.raises(prof.ProfileError):
            prof.Profile.from_dict({**base, "rw": parent})
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / "Descargas").mkdir()
    os.symlink(home / ".config", home / "Descargas" / "Windows")
    monkeypatch.setenv("HOME", str(home))
    p = prof.Profile.from_dict({**base, "rw": ["~/Descargas/Windows", "~/Descargas"]})
    args = prof.bwrap_args(p, uid=1000, runtime_dir="/run/user/1000", app_home=tmp_path, dbus_socket=None)
    assert str(home / ".config") not in args                      # el enlace a ~/.config no se monta
    assert str(home / "Descargas") in args
