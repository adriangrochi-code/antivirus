#!/usr/bin/env python3
"""Comprueba que cada paquete de iso/packages.list existe en Debian (suite y arquitecturas indicadas).
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
pkgs = [w for line in (Path(__file__).parent / "packages.list").read_text().splitlines()
        if not line.startswith("#") for w in line.split()]
missing = []
for p in pkgs:
    name, _, arch = p.partition(":")
    if name not in EXTERNOS and name not in names.get(arch or "amd64", set()):
        missing.append(p)
print(f"{len(pkgs)} paquetes comprobados en {SUITE}.")
if missing:
    print("NO EXISTEN:", " ".join(missing))
    sys.exit(1)
