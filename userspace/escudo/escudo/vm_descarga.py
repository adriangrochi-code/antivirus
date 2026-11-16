"""Descarga de la VM de análisis rápida después de instalar (no cabe en la ISO).

La ISO lleva solo la dirección y la huella SHA-256 del paquete (/usr/share/tisma/vm-analisis.conf, escritas al
construirla): lo descargado se comprueba contra esa huella antes de tocar nada, así que un servidor o una red
hostil no pueden colar otra cosa. Se descarga solo si el equipo puede usarla (KVM), con conexión no medida
(no gasta datos del móvil), y se reintenta más tarde si falla.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .detonar import VM_DIR, VM_RAPIDA

CONF = Path(os.environ.get("TISMA_VM_CONF", "/usr/share/tisma/vm-analisis.conf"))
MAX = 1 << 30


def leer_conf(path: Path = CONF) -> dict:
    d = {}
    for ln in path.read_text().splitlines():
        if "=" in ln and not ln.lstrip().startswith("#"):
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip().strip('"')
    if not re.fullmatch(r"https://[\w.-]+/[\w./%-]+", d.get("URL", "")):
        raise ValueError("URL no válida (solo https)")
    if not re.fullmatch(r"[0-9a-f]{64}", d.get("SHA256", "")):
        raise ValueError("SHA256 no válida")
    return d


def red_medida(run=subprocess.run) -> bool:
    """NetworkManager: Metered 1 (sí) o 3 (probablemente sí: datos del móvil compartidos)."""
    try:
        r = run(["busctl", "get-property", "org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager",
                 "org.freedesktop.NetworkManager", "Metered"], capture_output=True, text=True, timeout=10)
        return r.stdout.split()[-1] in ("1", "3")
    except (OSError, subprocess.SubprocessError, IndexError):
        return False


def _bajar(url: str, destino: Path) -> None:
    subprocess.run(["curl", "-fsSL", "--proto", "=https", "--tlsv1.2", "--retry", "3", "--max-filesize", str(MAX),
                    "-o", str(destino), url], check=True, timeout=3600)


def instalar(conf: Path = CONF, vm_dir: Path = VM_DIR, bajar=_bajar, kvm: bool | None = None,
             medida=red_medida) -> str:
    if all((vm_dir / f).exists() for f in VM_RAPIDA):
        return "ya instalada"
    if not conf.exists():
        return "sin configuración (esta ISO no la ofrece)"
    if not (os.path.exists("/dev/kvm") if kvm is None else kvm):
        return "sin KVM: Escudo usa su aislamiento normal"
    if medida():
        return "conexión medida: se intentará con otra red"
    c = leer_conf(conf)
    vm_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=vm_dir.parent, prefix=".vm-") as tmp:
        t = Path(tmp)
        paquete = t / "vm.tar"
        bajar(c["URL"], paquete)
        h = hashlib.sha256()
        with open(paquete, "rb") as fh:
            while b := fh.read(1 << 20):
                h.update(b)
        if h.hexdigest() != c["SHA256"]:
            raise ValueError("la huella no coincide: descarga descartada")
        with tarfile.open(paquete) as tar:
            miembros = {m.name: m for m in tar.getmembers()}
            if set(miembros) != set(VM_RAPIDA) or not all(m.isfile() for m in miembros.values()):
                raise ValueError("contenido inesperado en el paquete")
            for nombre in VM_RAPIDA:
                with tar.extractfile(miembros[nombre]) as src, open(t / nombre, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        for nombre in VM_RAPIDA:                        # el kernel el último: sin él la VM no se usa a medias
            os.chmod(t / nombre, 0o644)
        os.replace(t / "analisis.squashfs", vm_dir / "analisis.squashfs")
        os.replace(t / "vmlinuz-analisis", vm_dir / "vmlinuz-analisis")
    return "instalada"
