"""Descarga de la VM de análisis al instalar: solo con KVM, sin red medida, y solo si la huella coincide."""
import hashlib
import io
import shutil
import tarfile

import pytest

from escudo import vm_descarga as vd


def paquete(tmp_path, archivos=None):
    archivos = archivos or {"vmlinuz-analisis": b"kernel", "analisis.squashfs": b"raiz"}
    p = tmp_path / "vm.tar"
    with tarfile.open(p, "w") as t:
        for n, d in archivos.items():
            i = tarfile.TarInfo(n)
            i.size = len(d)
            t.addfile(i, io.BytesIO(d))
    return p


def conf(tmp_path, sha):
    c = tmp_path / "vm.conf"
    c.write_text(f"URL=https://github.com/x/y/releases/download/v1/vm.tar\nSHA256={sha}\n")
    return c


def correr(tmp_path, p, sha=None, **k):
    sha = sha or hashlib.sha256(p.read_bytes()).hexdigest()
    vm = tmp_path / "lib" / "vm"
    r = vd.instalar(conf(tmp_path, sha), vm, bajar=lambda url, dest: shutil.copy(p, dest),
                    kvm=k.get("kvm", True), medida=lambda: k.get("medida", False))
    return r, vm


def test_instala_con_huella_correcta(tmp_path):
    r, vm = correr(tmp_path, paquete(tmp_path))
    assert r == "instalada" and (vm / "vmlinuz-analisis").read_bytes() == b"kernel"
    assert (vm / "analisis.squashfs").stat().st_mode & 0o777 == 0o644
    assert correr(tmp_path, paquete(tmp_path))[0] == "ya instalada"


def test_huella_distinta_no_instala_nada(tmp_path):
    with pytest.raises(ValueError, match="huella"):
        correr(tmp_path, paquete(tmp_path), sha="0" * 64)
    assert not (tmp_path / "lib" / "vm" / "vmlinuz-analisis").exists()


def test_contenido_raro_rechazado(tmp_path):
    with pytest.raises(ValueError, match="contenido"):
        correr(tmp_path, paquete(tmp_path, {"../../etc/passwd": b"x", "vmlinuz-analisis": b"k"}))


def test_sin_kvm_o_red_medida_espera(tmp_path):
    assert correr(tmp_path, paquete(tmp_path), kvm=False)[0].startswith("sin KVM")
    assert correr(tmp_path, paquete(tmp_path), medida=True)[0].startswith("conexión medida")


def test_conf_solo_https(tmp_path):
    c = tmp_path / "c"
    c.write_text("URL=http://x.org/a\nSHA256=" + "a" * 64)
    with pytest.raises(ValueError):
        vd.leer_conf(c)
