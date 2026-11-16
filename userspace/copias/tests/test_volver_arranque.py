"""«Volver a como estaba antes de la última actualización» desde el menú de arranque (initramfs tisma-volver).
Prueba real sobre un btrfs en un archivo (necesita root y btrfs-progs; si no, se salta)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[3]
SCRIPT = RAIZ / "rootfs/etc/initramfs-tools/scripts/local-premount/tisma-volver"
NOMBRE = "20261116-100000-actualizacion"

pytestmark = pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("mkfs.btrfs"), reason="root + btrfs")


def sh(*a, **k):
    return subprocess.run(a, check=True, capture_output=True, text=True, **k)


@pytest.fixture
def disco(tmp_path):
    img = tmp_path / "b.img"
    img.write_bytes(b"")
    os.truncate(img, 300 << 20)
    sh("mkfs.btrfs", "-q", "-f", str(img))
    dev = sh("losetup", "-f", "--show", str(img)).stdout.strip()
    m = tmp_path / "m"
    m.mkdir()
    sh("mount", dev, str(m))
    sh("btrfs", "subvolume", "create", str(m / "@"))
    sh("btrfs", "subvolume", "create", str(m / "@snapshots"))
    (m / "@/estado").write_text("antes")
    sh("btrfs", "subvolume", "snapshot", "-r", str(m / "@"), str(m / "@snapshots" / NOMBRE))
    (m / "@/estado").write_text("roto")
    sh("umount", str(m))
    yield dev, m
    subprocess.run(["umount", str(m)], capture_output=True)
    subprocess.run(["losetup", "-d", dev], capture_output=True)


def _correr(tmp_path, dev, cmdline):
    (tmp_path / "cmdline").write_text(cmdline)
    s = SCRIPT.read_text().replace("/proc/cmdline", str(tmp_path / "cmdline")) \
        .replace("/run/tisma-volver-hecho", str(tmp_path / "hecho"))
    (tmp_path / "v.sh").write_text(s)
    subprocess.run(["sh", str(tmp_path / "v.sh")], env={**os.environ, "ROOT": dev}, check=True)


def _ver(dev, m):
    sh("mount", dev, str(m))
    try:
        return sorted(p.name for p in m.iterdir()), (m / "@/estado").read_text()
    finally:
        sh("umount", str(m))


def test_vuelve_una_sola_vez_y_guarda_la_de_antes(tmp_path, disco):
    dev, m = disco
    _correr(tmp_path, dev, f"quiet tisma.volver={NOMBRE}")
    nombres, estado = _ver(dev, m)
    assert estado == "antes"
    antes = [n for n in nombres if n.startswith("@antes-")]
    assert len(antes) == 1 and (tmp_path / "hecho").read_text().split() == [NOMBRE, antes[0]]
    _correr(tmp_path, dev, f"quiet tisma.volver={NOMBRE}")          # elegir la entrada otra vez: no repite
    assert len([n for n in _ver(dev, m)[0] if n.startswith("@antes-")]) == 1


def test_sin_parametro_o_nombre_raro_no_toca_nada(tmp_path, disco):
    dev, m = disco
    _correr(tmp_path, dev, "quiet splash")
    _correr(tmp_path, dev, "tisma.volver=../../@home")
    _correr(tmp_path, dev, "tisma.volver=20990101-000000-actualizacion")      # no existe
    nombres, estado = _ver(dev, m)
    assert estado == "roto" and not [n for n in nombres if n.startswith("@antes-")]
