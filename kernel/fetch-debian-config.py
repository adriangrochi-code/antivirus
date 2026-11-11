#!/usr/bin/env python3
"""Extrae la configuración del kernel de Debian verificando toda la cadena de confianza:
InRelease firmado (llavero oficial de Debian) → SHA-256 de Packages.xz → SHA-256 del .deb.
Uso: fetch-debian-config.py <mirror> <suite> <destino>"""
import hashlib
import io
import lzma
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# Claves oficiales de ftp-master.debian.org (archive-key-12, archive-key-13, release-13), fijadas en el repo
KEYRING = str(__import__("pathlib").Path(__file__).resolve().parent / "keys" / "debian-archive.gpg")


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as r:  # noqa: S310
        return r.read()


def main(mirror: str, suite: str, dest: str) -> None:
    mirror = mirror.rstrip("/")
    inrel = get(f"{mirror}/dists/{suite}/InRelease")
    with tempfile.NamedTemporaryFile() as f:
        f.write(inrel)
        f.flush()
        r = subprocess.run(["gpgv", "--keyring", KEYRING, "--output", "-", f.name], capture_output=True)
    if r.returncode != 0:
        sys.exit("Firma de InRelease inválida: " + r.stderr.decode()[-300:])
    signed = r.stdout.decode()                 # SOLO el contenido firmado
    m = re.search(r"^ ([0-9a-f]{64})\s+\d+\s+main/binary-amd64/Packages\.xz$", signed.split("SHA256:")[1], re.M)
    if not m:
        sys.exit("Packages.xz no figura en InRelease")
    pkgs_xz = get(f"{mirror}/dists/{suite}/main/binary-amd64/Packages.xz")
    if hashlib.sha256(pkgs_xz).hexdigest() != m.group(1):
        sys.exit("SHA-256 de Packages.xz incorrecto")
    pkgs = lzma.decompress(pkgs_xz).decode()
    cands = [s for s in pkgs.split("\n\n")
             if re.search(r"^Package: linux-image-\d[\w.+~-]*-amd64$", s, re.M)
             and not re.search(r"^Package: .*(dbg|unsigned|-rt-|cloud)", s, re.M)]
    if not cands:
        sys.exit("No se encontró el paquete del kernel de Debian")
    stanza = cands[-1]
    fname = re.search(r"^Filename: (\S+)$", stanza, re.M).group(1)
    sha = re.search(r"^SHA256: (\S+)$", stanza, re.M).group(1)
    deb = get(f"{mirror}/{fname}")
    if hashlib.sha256(deb).hexdigest() != sha:
        sys.exit("SHA-256 del paquete del kernel incorrecto")
    # .deb = ar(debian-binary, control.tar.*, data.tar.*)
    pos, data = 8, None
    while pos < len(deb):
        name, size = deb[pos:pos + 16].decode().strip(), int(deb[pos + 48:pos + 58].decode().strip())
        body = deb[pos + 60:pos + 60 + size]
        if name.startswith("data.tar"):
            data = body
        pos += 60 + size + (size & 1)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        member = next(x for x in t.getmembers() if re.search(r"boot/config-", x.name))
        Path(dest).write_bytes(t.extractfile(member).read())
    print(f"Configuración verificada: {member.name} → {dest}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
