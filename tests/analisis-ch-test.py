#!/usr/bin/env python3
"""VM de análisis con Cloud Hypervisor (VMM en Rust) en vez de QEMU: mismo aislamiento por hardware (KVM),
mucho menos código al alcance de la muestra, y foto/restauración incluidas.

REQUIERE KVM (/dev/kvm): no funciona en emulación. Mismo protocolo que tests/analisis-vm-precalentada.py con
DISCOS=1 (muestra por discos con número de serie, orden por la consola, semilla nueva al restaurar).

  preparar  <cloud-hypervisor> <ch-remote> <bzImage> <rootfs.sqfs> <dir>      arranca y guarda la foto en <dir>
  analizar  <cloud-hypervisor> <ch-remote> <bzImage> <rootfs.sqfs> <dir> [s]  restaura y analiza la muestra
  arrancar  <cloud-hypervisor> <ch-remote> <bzImage> <rootfs.sqfs> <dir> [s]  sin foto (arranque completo)

Los discos de entrada/salida y los sockets van en rutas FIJAS dentro de <dir> (la foto guarda las rutas); por eso
los análisis van de uno en uno. Cloud Hypervisor corre con seccomp (por defecto) y Landlock: solo puede abrir
el kernel, el sistema de análisis y su propia carpeta.
"""
from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location("precal", Path(__file__).with_name("analisis-vm-precalentada.py"))
precal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(precal)


def ch_argv(ch: str, kernel: str, sqfs: str, d: Path) -> list[str]:
    return [
        ch, "--api-socket", f"path={d / 'api.sock'}",
        "--cpus", "boot=2", "--memory", "size=512M",
        "--kernel", kernel,
        "--cmdline", "console=ttyS0 root=/dev/vda rootfstype=squashfs ro init=/usr/lib/tisma/init-analisis "
                     "tisma.espera lsm=landlock,lockdown,yama,apparmor lockdown=integrity panic=-1 quiet",
        "--disk", f"path={sqfs},readonly=on,id=raiz",
        f"path={d / 'entrada.img'},readonly=on,direct=on,serial=tisma-entrada,id=entrada",
        f"path={d / 'salida.img'},direct=on,serial=tisma-salida,id=salida",
        "--rng", "src=/dev/urandom",
        "--serial", f"socket={d / 'serie.sock'}", "--console", "off",
        "--net", "", "--seccomp", "true",
        "--landlock", "--landlock-rules", f"path={kernel},access=r", f"path={sqfs},access=r",
        f"path={d},access=rw",
    ]


def esperar_fin(p: subprocess.Popen, limite: float) -> None:
    try:
        p.wait(limite)
    except subprocess.TimeoutExpired:
        p.kill()
        raise


def preparar(ch: str, remote: str, kernel: str, sqfs: str, d: str) -> None:
    d = Path(d)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(mode=0o700, parents=True)
    vacia = Path(tempfile.mkdtemp())
    precal.crear_discos(vacia, d / "entrada.img", d / "salida.img")
    t0 = time.time()
    p = subprocess.Popen(ch_argv(ch, kernel, sqfs, d), stdout=subprocess.DEVNULL)
    try:
        serie = precal.conectar(d / "serie.sock")
        precal.esperar_texto(serie, b"TISMA-LISTO", 120)
        t_listo = time.time() - t0
        foto = d / "foto"
        foto.mkdir(mode=0o700)
        api = ["--api-socket", str(d / "api.sock")]
        subprocess.run([remote, *api, "pause"], check=True)
        subprocess.run([remote, *api, "snapshot", f"file://{foto}"], check=True)
        subprocess.run([remote, *api, "shutdown-vmm"], check=False)
        esperar_fin(p, 30)
        tam = sum(f.stat().st_size for f in foto.iterdir()) // 2**20
        print(f"preparada: arranque hasta listo {t_listo:.2f} s, foto {tam} MB")
    finally:
        p.kill()


def analizar(ch: str, remote: str, kernel: str, sqfs: str, d: str, segundos: int = 10, foto: bool = True) -> None:
    d = Path(d)
    w = Path(tempfile.mkdtemp(prefix="ch-muestra-"))
    precal.muestra_de_prueba(w)
    # Rutas fijas (las guarda la foto): se reponen los discos y se borran los sockets viejos
    precal.crear_discos(w, d / "entrada.img", d / "salida.img")
    for s in ("api.sock", "serie.sock"):
        (d / s).unlink(missing_ok=True)
    t0 = time.time()
    if foto:
        argv = [ch, "--api-socket", f"path={d / 'api.sock'}",
                "--restore", f"source_url=file://{d / 'foto'},resume=true",
                "--seccomp", "true", "--landlock", "--landlock-rules", f"path={kernel},access=r",
                f"path={sqfs},access=r", f"path={d},access=rw"]
    else:
        argv = ch_argv(ch, kernel, sqfs, d)
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL)
    try:
        serie = precal.conectar(d / "serie.sock")
        if not foto:
            precal.esperar_texto(serie, b"TISMA-LISTO", 120)
        t_lista = time.time() - t0
        serie.sendall(f"analizar {segundos} {secrets.token_hex(32)}\n".encode())
        esperar_fin(p, segundos + 120)
        total = time.time() - t0
    finally:
        p.kill()
    traza = precal.leer_salida(d / "salida.img")
    print(f"modo=cloud-hypervisor{'-foto' if foto else ''} lista_s={t_lista:.2f} total_s={total:.1f} "
          f"traza_lineas={len(traza.splitlines())} lee_senuelo={traza.count('id_ed25519')}")


if __name__ == "__main__":
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise SystemExit("Cloud Hypervisor necesita KVM (/dev/kvm): este equipo no lo tiene o no hay permiso")
    modo, *a = sys.argv[1:]
    if modo == "preparar":
        preparar(*a[:5])
    elif modo in ("analizar", "arrancar"):
        analizar(*a[:5], *(int(x) for x in a[5:6]), foto=(modo == "analizar"))
    else:
        raise SystemExit(__doc__)
