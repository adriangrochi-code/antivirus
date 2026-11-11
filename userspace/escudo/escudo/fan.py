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
