#!/usr/bin/env python3
"""VM de análisis precalentada: se arranca UNA vez, se guarda una foto de su memoria con todo listo, y cada
análisis restaura esa foto en vez de arrancar (kernel/analisis/README.md).

  preparar  <bzImage> <rootfs.sqfs> <foto>            arranca, espera «TISMA-LISTO» y guarda la foto
  analizar  <bzImage> <rootfs.sqfs> <foto> [segundos]  restaura la foto y analiza la muestra de prueba

La línea de QEMU tiene que ser la MISMA en los dos pasos (salvo -incoming y la carpeta de la muestra). La carpeta
9p no forma parte de la foto: el invitado la monta después de restaurar. Con la orden va una semilla nueva: el
invitado la mezcla y fuerza la renovación de su generador aleatorio (si no, todos los análisis lo compartirían).
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ENTRADA_MB, SALIDA_MB = 64, 16      # tamaño FIJO: tras restaurar la foto el invitado debe ver el mismo disco
DISCOS = bool(os.environ.get("DISCOS"))   # DISCOS=1: la muestra va por discos (sin 9p)


def crear_discos(carpeta: Path, entrada: Path, salida: Path) -> None:
    """Entrada: tar de orden.json + muestra/ (relleno hasta ENTRADA_MB). Salida: ceros (SALIDA_MB)."""
    import tarfile
    with tarfile.open(entrada, "w", format=tarfile.USTAR_FORMAT) as t:
        for nombre in ("orden.json", "muestra"):
            if (carpeta / nombre).exists():
                t.add(carpeta / nombre, arcname=nombre)
    if entrada.stat().st_size > ENTRADA_MB << 20:
        raise SystemExit("la muestra no cabe en el disco de entrada")
    os.truncate(entrada, ENTRADA_MB << 20)
    with open(salida, "wb") as f:
        f.truncate(SALIDA_MB << 20)


def leer_salida(salida: Path) -> str:
    """Solo se interpreta una cabecera fija y una longitud acotada: lo demás es texto sin confianza."""
    with open(salida, "rb") as f:
        cab = f.readline(64)
        if not cab.startswith(b"TISMA-TRAZA "):
            return ""
        n = int(cab.split()[1]) if cab.split()[1].isdigit() else 0
        return f.read(min(n, SALIDA_MB << 20)).decode(errors="replace")


def qemu_argv(kernel: str, sqfs: str, carpeta: Path, serie: Path, qmp: Path,
              discos: tuple[Path, Path] | None = None) -> list[str]:
    kvm = os.access("/dev/kvm", os.W_OK)
    # sin KVM (solo pruebas) no hay reloj de referencia: frecuencia fija para que el arranque no se cuelgue
    tsc = "" if kvm else " tsc_early_khz=2000000"
    return [
        "qemu-system-x86_64", *(["-enable-kvm", "-cpu", "host"] if kvm else []),
        "-M", "microvm", "-m", "512", "-smp", "2", "-display", "none", "-no-reboot", "-nodefaults",
        "-no-user-config", "-nic", "none",
        "-chardev", f"socket,id=s0,path={serie},server=on,wait=off", "-serial", "chardev:s0",
        "-qmp", f"unix:{qmp},server=on,wait=off",
        "-kernel", kernel,
        "-append", "console=ttyS0 root=/dev/vda rootfstype=squashfs ro init=/usr/lib/tisma/init-analisis "
                   "tisma.espera lsm=landlock,lockdown,yama,apparmor lockdown=integrity panic=-1 quiet "
                   "no_timer_check pci=off" + tsc,
        "-drive", f"id=raiz,file={sqfs},format=raw,if=none,readonly=on", "-device", "virtio-blk-device,drive=raiz",
        *(["-drive", f"id=ent,file={discos[0]},format=raw,if=none,readonly=on,cache=none",
            "-device", "virtio-blk-device,drive=ent,serial=tisma-entrada",
            "-drive", f"id=sal,file={discos[1]},format=raw,if=none,cache=none",
            "-device", "virtio-blk-device,drive=sal,serial=tisma-salida"] if discos else
          ["-fsdev", f"local,id=m,path={carpeta},security_model=mapped-xattr",
           "-device", "virtio-9p-device,fsdev=m,mount_tag=muestra"]),
        "-device", "virtio-rng-device",
    ]


def conectar(ruta: Path, limite: float = 30) -> socket.socket:
    t = time.time()
    while True:
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(ruta))
            return s
        except OSError:
            if time.time() - t > limite:
                raise
            time.sleep(0.05)


def qmp(s: socket.socket, orden: str, **args) -> dict:
    s.sendall(json.dumps({"execute": orden, **({"arguments": args} if args else {})}).encode() + b"\n")
    buf = b""
    while True:
        buf += s.recv(65536)
        for linea in buf.split(b"\n"):
            if linea.strip():
                m = json.loads(linea)
                if "return" in m or "error" in m:
                    return m
        buf = buf[buf.rfind(b"\n") + 1:] if b"\n" in buf else buf


def esperar_texto(s: socket.socket, texto: bytes, limite: float) -> bytes:
    s.settimeout(1)
    salida, t = b"", time.time()
    while texto not in salida:
        if time.time() - t > limite:
            raise TimeoutError(f"no apareció {texto!r}; consola: {salida[-500:]!r}")
        try:
            trozo = s.recv(65536)
        except socket.timeout:
            continue
        if not trozo:
            break
        salida += trozo
    return salida


def muestra_de_prueba(w: Path) -> None:
    (w / "muestra").mkdir()
    m = w / "muestra" / "muestra.sh"
    m.write_text('#!/bin/sh\ncat "$HOME/.ssh/id_ed25519" > /dev/null\necho hola > /tmp/x\n'
                 # ALEATORIO=1: crea un archivo con 16 bytes de /dev/urandom en el nombre (strace lo deja en la traza) para comprobar que
                 # dos análisis restaurados de la misma foto NO reciben los mismos números aleatorios
                 + (': > "/tmp/ALEATORIO=$(od -An -N16 -tx1 /dev/urandom | tr -d " \\n")"\n'
                    if os.environ.get("ALEATORIO") else ""))
    m.chmod(0o755)
    (w / "orden.json").write_text('{"muestra": "muestra.sh", "args": []}')


def preparar(kernel: str, sqfs: str, foto: str) -> None:
    w = Path(tempfile.mkdtemp(prefix="precal-"))
    t0 = time.time()
    ctl = Path(tempfile.mkdtemp(prefix="precal-ctl-"))   # control de QEMU FUERA de la carpeta compartida
    discos = (ctl / "entrada.img", ctl / "salida.img") if DISCOS else None
    if discos:
        crear_discos(w, *discos)                         # vacíos: solo para que la foto tenga los mismos discos
    p = subprocess.Popen(qemu_argv(kernel, sqfs, w, ctl / "serie", ctl / "qmp", discos), stdout=subprocess.DEVNULL)
    try:
        serie = conectar(ctl / "serie")
        esperar_texto(serie, b"TISMA-LISTO", 300)
        t_listo = time.time() - t0
        c = conectar(ctl / "qmp")
        qmp(c, "qmp_capabilities")
        qmp(c, "stop")
        r = qmp(c, "migrate", uri=f"file:{foto}")
        if "error" in r:
            raise SystemExit(f"no se pudo guardar la foto: {r['error']}")
        while (estado := qmp(c, "query-migrate")["return"].get("status")) not in ("completed", "failed"):
            time.sleep(0.1)
        qmp(c, "quit")
        p.wait(30)
        os.chmod(foto, 0o600)
        print(f"preparada: arranque hasta listo {t_listo:.1f} s, foto {os.path.getsize(foto) // 2**20} MB ({estado})")
    finally:
        p.kill()


def analizar(kernel: str, sqfs: str, foto: str, segundos: int = 10) -> None:
    w = Path(tempfile.mkdtemp(prefix="precal-"))
    muestra_de_prueba(w)
    t0 = time.time()
    ctl = Path(tempfile.mkdtemp(prefix="precal-ctl-"))   # control de QEMU FUERA de la carpeta compartida
    discos = (ctl / "entrada.img", ctl / "salida.img") if DISCOS else None
    if discos:
        crear_discos(w, *discos)
    p = subprocess.Popen([*qemu_argv(kernel, sqfs, w, ctl / "serie", ctl / "qmp", discos),
                          "-incoming", f"file:{foto}"], stdout=subprocess.DEVNULL)
    try:
        c = conectar(ctl / "qmp")
        qmp(c, "qmp_capabilities")
        # La foto se guardó con la VM en pausa: al terminar de cargarla sigue en pausa hasta «cont»
        while (estado := qmp(c, "query-status")["return"]["status"]) == "inmigrate":
            time.sleep(0.02)
        t_restaurada = time.time() - t0
        if estado != "running":
            qmp(c, "cont")
        serie = conectar(ctl / "serie")
        serie.sendall(f"analizar {segundos} {secrets.token_hex(32)}\n".encode())
        serie.settimeout(1)
        p.wait(segundos + 300)
        total = time.time() - t0
    finally:
        p.kill()
    if discos:
        traza = leer_salida(discos[1])
    else:
        traza = (w / "traza.txt").read_text(errors="replace") if (w / "traza.txt").exists() else ""
    print(f"modo=precalentado restaurada_s={t_restaurada:.2f} total_s={total:.1f} traza_lineas={len(traza.splitlines())} "
          f"lee_senuelo={traza.count('id_ed25519')}")
    if os.environ.get("ALEATORIO"):
        import re
        print("aleatorio=" + ",".join(sorted(set(re.findall(r"ALEATORIO=([0-9a-f]{32})", traza)))))


if __name__ == "__main__":
    modo, *a = sys.argv[1:]
    if modo == "preparar":
        preparar(*a[:3])
    elif modo == "analizar":
        analizar(*a[:3], *(int(x) for x in a[3:4]))
    else:
        raise SystemExit(__doc__)
