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


# ---------------------------------------------------------------- zona segura desechable
from tisma_sandbox import zona_segura as zs  # noqa: E402


def test_zona_visor_por_tipo():
    assert zs.visor_para(Path("a.PDF"), existe=lambda p: True)[0] == "/usr/bin/okular"
    assert zs.visor_para(Path("f.docx"), existe=lambda p: True)[1][-1] == "--view"     # solo lectura
    assert zs.visor_para(Path("v.mp4"), existe=lambda p: True)[2] is True               # vídeo: con sonido
    with pytest.raises(zs.NoSePuede):
        zs.visor_para(Path("x.exe"), existe=lambda p: True)                              # programas: Escudo


def test_zona_perfil_minimo(tmp_path):
    p = zs.perfil("zona-abc", "/usr/bin/okular", audio=False)
    assert not (p.red or p.gpu or p.camara or p.mandos or p.portales or p.x11 or p.audio)
    assert p.rw == p.ro == p.dbus_talk == p.dbus_own == []
    a = prof.bwrap_args(p, uid=1000, runtime_dir="/run/user/1000", app_home=tmp_path, dbus_socket=None)
    assert "--unshare-net" in a and "DBUS_SESSION_BUS_ADDRESS" not in a
    assert ["--bind", str(tmp_path), str(Path.home())] == a[a.index("--bind"):a.index("--bind") + 3]


def test_zona_copia_y_borra_todo(tmp_path, monkeypatch):
    monkeypatch.setattr(zs, "visor_para", lambda r: ("/usr/bin/okular", [], False))
    monkeypatch.setattr(zs, "avisar", lambda *a: None)
    original = tmp_path / "factura.pdf"
    original.write_bytes(b"%PDF-1.4 dudoso")
    base = tmp_path / "run"
    base.mkdir()
    vistos = {}

    def ejecutar(p, argv, app_home):
        vistos["argv"], vistos["home"] = argv, app_home
        assert (app_home / "archivo" / "factura.pdf").read_bytes() == b"%PDF-1.4 dudoso"
        (app_home / "archivo" / "factura.pdf").write_bytes(b"cambiado dentro")      # no llega al original
        return 0

    assert zs.abrir(original, ejecutar=ejecutar, base=str(base)) == 0
    assert vistos["argv"][-1].endswith("/archivo/factura.pdf")
    assert original.read_bytes() == b"%PDF-1.4 dudoso" and list(base.iterdir()) == []   # nada queda


def test_zona_rechaza_lo_que_no_es_archivo(tmp_path):
    with pytest.raises(zs.NoSePuede):
        zs.copiar(tmp_path, tmp_path / "x")


@pytest.mark.skipif(not shutil.which("bwrap") or subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"],
                    capture_output=True).returncode != 0, reason="sin bubblewrap utilizable")
def test_zona_de_verdad_no_ve_tus_archivos(tmp_path, monkeypatch):
    """Dentro de la zona, el programa ve SOLO la copia: un archivo de tu carpeta personal no existe ahí."""
    secreto = Path.home() / ".tisma-prueba-zona"
    secreto.write_text("secreto")
    try:
        doc = tmp_path / "doc.txt"
        doc.write_text("hola")
        p = zs.perfil("zona-test", "/bin/sh", audio=False)
        app_home = tmp_path / "home"
        zs.copiar(doc, app_home / "archivo")
        from tisma_sandbox.cli import run
        r = run(p, ["-c", f"cat ~/archivo/doc.txt && ! test -e {secreto} && ! test -e ~/.tisma-prueba-zona"],
                app_home=app_home)
        assert r == 0
    finally:
        secreto.unlink()
