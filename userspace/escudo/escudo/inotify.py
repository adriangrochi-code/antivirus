"""inotify mínimo (ctypes, sin dependencias): esperar a que algo cambie en vez de mirar cada poco.

Un bucle «leo; si no hay nada, duermo 0,3 s» despierta la CPU cientos de miles de veces al día aunque no pase
nada; con inotify el hilo duerme hasta que el kernel avisa de un cambio.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import select

IN_MODIFY, IN_MOVED_TO, IN_CREATE = 0x2, 0x80, 0x100
IN_NONBLOCK, IN_CLOEXEC = 0o4000, 0o2000000


class Watch:
    """Descriptor que se vuelve legible cuando cambia algo en `directory`. Si inotify no está disponible,
    `wait` se comporta como un sondeo con el intervalo de respaldo."""

    def __init__(self, directory: str, fallback_s: float = 1.0):
        self.fd = -1
        self.fallback_s = fallback_s
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
            if fd >= 0 and libc.inotify_add_watch(fd, directory.encode(), IN_MODIFY | IN_MOVED_TO | IN_CREATE) >= 0:
                self.fd = fd
            elif fd >= 0:
                os.close(fd)
        except (OSError, AttributeError):
            pass

    def wait(self, timeout: float) -> None:
        if self.fd < 0:
            select.select([], [], [], min(timeout, self.fallback_s))
            return
        select.select([self.fd], [], [], timeout)
        try:
            while os.read(self.fd, 4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
