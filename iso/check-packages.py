#!/usr/bin/env python3
"""Comprueba que cada paquete de iso/packages.list y de los perfiles (rootfs/usr/share/tisma/perfiles) existe en Debian (suite y arquitecturas indicadas).
Evita construir una imagen a la que le faltan piezas en silencio (build-iso.sh omite los que no encuentra).
Uso: check-packages.py [mirror] [suite]"""
import lzma
import re
import sys
import urllib.request
from pathlib import Path

MIRROR = sys.argv[1] if len(sys.argv) > 1 else "https://deb.debian.org/debian"
SUITE = sys.argv[2] if len(sys.argv) > 2 else "trixie"
EXTERNOS = {"python3-gbinder"}          # repositorio de Waydroid
names: dict[str, set] = {}
for comp in ("main", "contrib", "non-free", "non-free-firmware"):
    for arch in ("amd64", "i386"):
        url = f"{MIRROR}/dists/{SUITE}/{comp}/binary-{arch}/Packages.xz"
        try:
            data = lzma.decompress(urllib.request.urlopen(url, timeout=120).read()).decode(errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"^(?:Package|Provides): (.+)$", data, re.M):
            for x in m.group(1).split(","):
                names.setdefault(arch, set()).add(x.strip().split()[0])
HERE = Path(__file__).resolve().parent
listas = [HERE / "packages.list", *sorted((HERE.parent / "rootfs/usr/share/tisma/perfiles").glob("*.list"))]
# «@nombre» = componente descargado de su release (no es de Debian)
pkgs = list(dict.fromkeys(w for f in listas for line in f.read_text().splitlines()
                          for w in line.split("#")[0].split() if not w.startswith("@")))
missing = []
for p in pkgs:
    name, _, arch = p.partition(":")
    if name not in EXTERNOS and name not in names.get(arch or "amd64", set()):
        missing.append(p)
print(f"{len(pkgs)} paquetes comprobados en {SUITE}.")
if missing:
    print("NO EXISTEN:", " ".join(missing))
    sys.exit(1)
