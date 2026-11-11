"""Compresión de memoria con política propia: comprimir ANTES y lo que NO se usa (como Android con sus apps en
segundo plano), en vez de lo que hace Linux por defecto: esperar a que la RAM se llene y entonces comprimir con
prisa lo que pille, cuando el equipo ya va a tirones.

* Cada app del escritorio vive en su propio cgroup (app-*.scope / app-*.service bajo user@UID.service). Si una
  lleva 10 minutos sin usar CPU (minimizada, en otra pestaña del escritorio, olvidada) y la RAM libre baja de la
  mitad, se le pide al kernel que comprima su memoria FRÍA (memory.reclaim: MGLRU elige las páginas menos
  usadas; con zram van comprimidas a RAM, no al disco). Si vuelves a ella, se descomprime al tocarla (lz4: GB/s).
* Nunca se toca lo que no debe esperar: Plasma, KWin, Xwayland, audio, el motor de IA ni lo que está usando CPU.
* Por app, como mucho una vez cada 15 minutos y como mucho la mitad de su memoria anónima (máx. 1 GB).
"""

from __future__ import annotations

import logging
import os
import re
import time

from . import sysfs

log = logging.getLogger("centinela.memoria")

IDLE_S = 600                  # sin CPU durante 10 min
COOLDOWN_S = 900              # una vez cada 15 min por app
MIN_ANON = 64 << 20           # no merece la pena por menos de 64 MB
MAX_RECLAIM = 1 << 30
PROTECTED = re.compile(r"(?i)plasma|kwin|xwayland|pipewire|wireplumber|pulseaudio|llama|strata|tisma-code|"
                       r"tisma_code|kded|ksmserver|polkit|xdg-desktop-portal|sddm|gamescope|steam_app")
APP_GLOB = "/sys/fs/cgroup/user.slice/user-*.slice/user@*.service/app.slice/*"


def _stat(cg: str, name: str, key: str) -> int | None:
    for line in (sysfs.read(f"{cg}/{name}", "") or "").splitlines():
        k, _, v = line.partition(" ")
        if k == key:
            try:
                return int(v)
            except ValueError:
                return None
    return None


class Compactor:
    def __init__(self, now=time.monotonic, write=None):
        self.now = now
        self.write = write or self._write
        self.seen: dict[str, tuple[int, float]] = {}     # cgroup → (uso de CPU, último momento con actividad)
        self.last: dict[str, float] = {}                 # cgroup → última compactación

    @staticmethod
    def _write(path: str, value: str) -> bool:
        try:
            with open(sysfs.path(path), "w") as fh:
                fh.write(value)
            return True
        except OSError as e:                             # EAGAIN: el kernel no pudo liberar tanto; no pasa nada
            log.debug("memory.reclaim %s: %s", path, e)
            return False

    def step(self, avail_mb: float, total_mb: float) -> list[tuple[str, int]]:
        """Revisa las apps; devuelve [(app, bytes pedidos)] de las que se compactaron en esta pasada."""
        now = self.now()
        done = []
        live = set()
        for cg in sysfs.ls(APP_GLOB):
            name = os.path.basename(cg)
            live.add(cg)
            usage = _stat(cg, "cpu.stat", "usage_usec")
            if usage is None:
                continue
            prev = self.seen.get(cg)
            if prev is None or usage != prev[0]:
                self.seen[cg] = (usage, now)             # hubo actividad
                continue
            if avail_mb >= total_mb * 0.5 or PROTECTED.search(name):
                continue                                 # sobra RAM, o no se toca
            if now - prev[1] < IDLE_S or now - self.last.get(cg, -1e18) < COOLDOWN_S:
                continue
            anon = _stat(cg, "memory.stat", "anon") or 0
            if anon < MIN_ANON:
                continue
            amount = min(anon // 2, MAX_RECLAIM)
            self.last[cg] = now
            if self.write(f"{cg}/memory.reclaim", str(amount)):
                done.append((name, amount))
                log.info("Memoria fría de %s comprimida (%d MB): lleva %d min sin usarse", name, amount >> 20,
                         (now - prev[1]) // 60)
        for gone in [c for c in self.seen if c not in live]:
            self.seen.pop(gone, None)
            self.last.pop(gone, None)
        return done
