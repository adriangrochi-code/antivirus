"""tisma-copias con btrfs/findmnt/restic simulados."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_copias import core  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


class Fake:
    def __init__(self, fstype="btrfs", root_subvol="/@", home_subvol="/@home"):
        self.calls = []
        self.mounts = {"/": (fstype, f"/dev/mapper/luks-x[{root_subvol}]", f"rw,noatime,subvol={root_subvol}"),
                       "/home": (fstype, "/dev/mapper/luks-x[/@home]", f"rw,subvol={home_subvol}")}

    def __call__(self, argv, env=None, check=True):
        self.calls.append((argv, env))
        if argv[0] == "findmnt":
            target = argv[-1]
            m = self.mounts.get(target, ("btrfs", "/dev/mapper/luks-x", "rw,subvol=/@snapshots"))
            return subprocess.CompletedProcess(argv, 0, " ".join(m) + "\n", "")
        if argv[:3] == ["btrfs", "subvolume", "snapshot"]:
            Path(argv[-1]).mkdir(parents=True)
        if argv[:3] == ["btrfs", "subvolume", "delete"]:
            Path(argv[-1]).rmdir()
        if argv[0] == "cp":
            subprocess.run(argv, check=True)
        if argv[:2] == ["restic", "init"]:
            Path(env["RESTIC_REPOSITORY"], "config").write_text("x")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_create_list_and_prune(tmp_path, monkeypatch):
    fake = Fake()
    t = [dt.datetime(2026, 11, 1, 3, 0, 0)]
    monkeypatch.setattr(core, "_now", lambda: t[0])
    for i in range(9):
        t[0] = dt.datetime(2026, 11, 1 + i, 3, 0, 0)
        assert core.crear("diaria", runner=fake, snapdir=tmp_path)
    snaps = core.instantaneas(tmp_path)
    assert len(snaps) == 9 and all(s["home"] for s in snaps)
    ro = [c[0] for c in fake.calls if c[0][:3] == ["btrfs", "subvolume", "snapshot"]]
    assert all("-r" in c for c in ro)                       # siempre de solo lectura
    gone = core.podar(runner=fake, snapdir=tmp_path)
    assert len(gone) == 2 and gone[0].startswith("20261101")
    assert len(core.instantaneas(tmp_path)) == 7


def test_update_hook_deduplicates(tmp_path, monkeypatch):
    fake = Fake()
    t = dt.datetime(2026, 11, 11, 10, 0, 0)
    monkeypatch.setattr(core, "_now", lambda: t)
    assert core.crear("actualizacion", runner=fake, snapdir=tmp_path)
    t = t + dt.timedelta(minutes=2)
    assert core.crear("actualizacion", runner=fake, snapdir=tmp_path) is None


def test_requires_btrfs(tmp_path):
    with pytest.raises(core.CopiasError, match="btrfs"):
        core.crear("manual", runner=Fake(fstype="ext4"), snapdir=tmp_path)


def test_restore_file_keeps_current(tmp_path, monkeypatch):
    fake = Fake()
    snapdir, live = tmp_path / "snaps", tmp_path / "vivo"
    snapdir.mkdir()
    live.mkdir()
    monkeypatch.setattr(core, "_now", lambda: dt.datetime(2026, 11, 11, 12, 0, 0))
    name = core.crear("manual", runner=fake, snapdir=snapdir)
    doc = live / "informe.txt"
    old = snapdir / name / str(doc).lstrip("/")
    old.parent.mkdir(parents=True)
    old.write_text("versión buena")
    doc.write_text("cifrado por ransomware")
    core.restaurar_archivo(name, str(doc), runner=fake, snapdir=snapdir)
    assert doc.read_text() == "versión buena"
    assert (live / "informe.txt.antes-20261111-120000").read_text() == "cifrado por ransomware"
    with pytest.raises(core.CopiasError):
        core.restaurar_archivo("../../etc", str(doc), runner=fake, snapdir=snapdir)
    link = tmp_path / "enlace"
    link.symlink_to(live)
    with pytest.raises(core.CopiasError, match="enlace"):
        core.restaurar_archivo(name, str(link / "informe.txt"), runner=fake, snapdir=snapdir)


def test_rollback_swaps_subvolumes(tmp_path, monkeypatch):
    fake = Fake()
    top, snapdir = tmp_path / "top", tmp_path / "top" / "@snapshots"
    (top / "@").mkdir(parents=True)
    snapdir.mkdir()
    (snapdir / "20261110-030000-diaria").mkdir()
    monkeypatch.setattr(core, "_now", lambda: dt.datetime(2026, 11, 11, 9, 0, 0))
    old = core.volver_atras("20261110-030000-diaria", runner=fake, snapdir=snapdir, topmnt=top)
    assert old == "@antes-20261111-090000" and (top / old).is_dir() and (top / "@").is_dir()
    assert any(c[0][:2] == ["mount", "-o"] and c[0][3] == "/dev/mapper/luks-x" for c in fake.calls)
    assert any(s["motivo"] == "antes-restaurar" for s in core.instantaneas(snapdir))
    with pytest.raises(core.CopiasError, match="@"):
        core.volver_atras("20261110-030000-diaria", runner=Fake(root_subvol="/root"), snapdir=snapdir, topmnt=top)


def test_external_backup(tmp_path):
    fake = Fake()
    cfg, key, dest = tmp_path / "etc" / "copias.json", tmp_path / "etc" / "clave", tmp_path / "usb" / "tisma"
    clave, same = core.configurar_externo(str(dest), runner=fake, cfg=cfg, keyfile=key)
    assert len(clave) == 41 and oct(key.stat().st_mode & 0o777) == "0o600"
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    assert core.configurar_externo(str(dest), runner=fake, cfg=cfg, keyfile=key)[0] == clave   # misma clave
    msg = core.copiar_externo(runner=fake, cfg=cfg, keyfile=key)
    assert "hecha" in msg
    backup = [c for c in fake.calls if c[0][:2] == ["restic", "backup"]][0]
    assert "/home" in backup[0] and "/home/*/.cache" in backup[0]
    assert backup[1]["RESTIC_PASSWORD_FILE"] == str(key) and "RESTIC_PASSWORD" not in backup[1]
    (dest / "config").unlink()                  # disco desconectado
    assert "no está conectado" in core.copiar_externo(runner=fake, cfg=cfg, keyfile=key)
    with pytest.raises(core.CopiasError):
        core.configurar_externo("relativa/ruta", runner=fake, cfg=cfg, keyfile=key)
    with pytest.raises(core.CopiasError):
        core.restaurar_externo("abc; rm -rf /", str(tmp_path / "r"), runner=fake, cfg=cfg, keyfile=key)


def test_shipped_units_and_hook():
    apt = (ROOT / "rootfs/etc/apt/apt.conf.d/80-tisma-instantanea").read_text()
    assert "--motivo actualizacion --silencioso" in apt
    for unit in ("tisma-copias-instantanea", "tisma-copias-externo"):
        svc = (ROOT / f"rootfs/etc/systemd/system/{unit}.service").read_text()
        assert "Slice=tisma-segundo-plano.slice" in svc and "ProtectHome" not in svc
        assert (ROOT / f"rootfs/etc/systemd/system/{unit}.timer").exists()
    mount = (ROOT / "rootfs/etc/calamares/modules/mount.conf").read_text()
    assert "/@snapshots" in mount and "/.snapshots" in mount
    assert 'defaultFileSystemType: "btrfs"' in (ROOT / "rootfs/etc/calamares/modules/partition.conf").read_text()
    assert os.access(ROOT / "rootfs/usr/bin/tisma-copias", os.X_OK)
    pol = (ROOT / "rootfs/usr/share/polkit-1/actions/org.tisma.copias.policy").read_text()
    assert "<allow_active>auth_admin</allow_active>" in pol and "/usr/bin/tisma-copias" in pol
