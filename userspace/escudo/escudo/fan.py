"""Piezas comunes de fanotify para la puerta de primera ejecución y la protección en tiempo real.

Marca por SISTEMA DE ARCHIVOS (FAN_MARK_FILESYSTEM), no por montaje: escudo.service corre en su propio espacio
de montajes (ProtectSystem=strict) y los programas del usuario, Flatpak o bubblewrap en otros; una marca por
montaje solo ve los accesos hechos a través de ESE montaje, así que desde el servicio no veía nada del usuario
(comprobado con unshare -m: con FAN_MARK_MOUNT llegaban 0 eventos del anfitrión, con FAN_MARK_FILESYSTEM sí).

Una marca por sistema de archivos ve todo el disco (en btrfs, todos los subvolúmenes), así que cada evento se
filtra primero por dispositivo con un fstat (los subvolúmenes de btrfs tienen st_dev propio) y solo después se
lee su ruta.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import select
import struct

log = logging.getLogger("escudo.fan")

FAN_MARK_ADD, FAN_MARK_MOUNT, FAN_MARK_FILESYSTEM = 0x1, 0x10, 0x100
FAN_Q_OVERFLOW = 0x4000
AT_FDCWD = -100
META = struct.Struct("<IBBHQii")          # fanotify_event_metadata


def libc():
    return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def mark(lib, fd: int, path: str, mask: int) -> str | None:
    """Marca el sistema de archivos de `path`; si el kernel no lo permite, su montaje (con aviso).
    Devuelve "fs", "montaje" o None."""
    if lib.fanotify_mark(fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM, ctypes.c_uint64(mask), AT_FDCWD, path.encode()) == 0:
        return "fs"
    err = ctypes.get_errno()
    if lib.fanotify_mark(fd, FAN_MARK_ADD | FAN_MARK_MOUNT, ctypes.c_uint64(mask), AT_FDCWD, path.encode()) == 0:
        log.warning("%s: marca por sistema de archivos no permitida (errno %d); se usa la del montaje, que no ve "
                    "lo que ocurre dentro de sandboxes ni de otros espacios de montaje", path, err)
        return "montaje"
    log.warning("fanotify no pudo vigilar %s (errno %d)", path, ctypes.get_errno())
    return None


def submounts(roots: list[str], mountinfo: str = "/proc/self/mountinfo") -> list[str]:
    """Puntos de montaje por debajo de las raíces (p. ej. /home/usuario en otro volumen)."""
    out = []
    try:
        for line in open(mountinfo, encoding="utf-8", errors="replace"):
            mp = line.split()[4].replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n")
            if any(mp != r and mp.startswith(r.rstrip("/") + "/") for r in roots):
                out.append(mp)
    except OSError:
        pass
    return out


def devices(paths: list[str]) -> set[int]:
    devs = set()
    for p in paths:
        try:
            devs.add(os.stat(p).st_dev)
        except OSError:
            continue
    return devs


def inside(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def fd_path(efd: int, st: os.stat_result | None = None) -> str:
    """Ruta del archivo de un evento. « (deleted)» solo se quita si de verdad está borrado (un archivo puede
    llamarse así a propósito para que su ruta no exista al analizarlo)."""
    path = os.readlink(f"/proc/self/fd/{efd}")
    if path.endswith(" (deleted)"):
        st = st or os.fstat(efd)
        if st.st_nlink == 0:
            path = path[:-10]
    return path


# ---------------------------------------------------------------- parada segura de los hilos lectores
class Despertador:
    """Tubería para despertar a un hilo bloqueado esperando eventos. Así stop() puede esperar a que el hilo
    termine ANTES de cerrar el descriptor de fanotify: cerrarlo con el hilo dentro de read() o write() podía
    hacer que el número del descriptor se reutilizara para otro archivo y el hilo leyera (y cerrara
    descriptores sacados de) o escribiera la respuesta en ese otro archivo."""

    def __init__(self):
        self.r, self.w = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)

    def despertar(self) -> None:
        try:
            os.write(self.w, b"x")
        except OSError:
            pass

    def cerrar(self) -> None:
        for fd in (self.r, self.w):
            try:
                os.close(fd)
            except OSError:
                pass


def esperar(fd: int, despertador: Despertador, eventos: int = select.POLLIN, timeout_ms: int = -1) -> bool:
    """True si hay algo en «fd» (o pasó el tiempo); False si se pidió parar."""
    p = select.poll()
    p.register(fd, eventos)
    p.register(despertador.r, select.POLLIN)
    while True:
        try:
            listos = p.poll(timeout_ms)
        except InterruptedError:
            continue
        return not any(f == despertador.r for f, _ in listos)


def parar_hilos(hilos: list, fd: int, despertador: Despertador | None, log: logging.Logger,
                espera: float = 5.0) -> int:
    """Despierta y espera a los hilos; solo entonces cierra el descriptor. Devuelve -1 (el nuevo valor de fd)."""
    if despertador:
        despertador.despertar()
    vivos = []
    for h in hilos:
        h.join(espera)
        if h.is_alive():
            vivos.append(h.name)
    if vivos:
        # Mejor no cerrar que arriesgar escribir en otro archivo: el proceso está terminando y el kernel lo cierra
        log.warning("Hilos de fanotify sin terminar (%s): el descriptor se deja abierto", ", ".join(vivos))
        return -1
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass
    if despertador:
        despertador.cerrar()
    return -1
