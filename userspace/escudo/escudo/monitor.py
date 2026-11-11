"""Protección en tiempo real: escanea archivos nuevos o modificados."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import struct
import threading
import time

from .quarantine import Quarantine
from .scanner import Detection, Scanner

log = logging.getLogger("escudo.monitor")

FAN_CLOEXEC, FAN_CLASS_NOTIF = 0x1, 0x0
FAN_MARK_ADD, FAN_MARK_MOUNT = 0x1, 0x10
FAN_CLOSE_WRITE = 0x8
O_LARGEFILE = 0o100000
AT_FDCWD = -100
META = struct.Struct("<IBBHQii")          # fanotify_event_metadata
# Descargas del navegador y guardados atómicos: se escriben con un nombre temporal y luego se renombran
TEMP_SUFFIX = (".crdownload", ".part", ".download", ".partial", ".tmp")


class FanotifyWatcher:
    """Archivos cerrados tras escribirse en uno o más puntos de montaje, con UNA marca por montaje.

    inotify (watchdog) necesita una vigilancia por CADA carpeta: con un /home grande (node_modules, cachés,
    proyectos) son decenas de miles, cada una retiene su inodo en la memoria del kernel, y al arrancar hay que
    recorrer todo el árbol. fanotify sobre el montaje cuesta lo mismo tenga /home cien carpetas o un millón.
    """

    def __init__(self, on_path):
        self.on_path = on_path
        self.fd = -1
        self.covered: list[str] = []
        self._stop = threading.Event()

    def start(self, paths: list[str]) -> list[str]:
        """Vigila las rutas que son puntos de montaje; devuelve cuáles (el resto queda para inotify)."""
        mounts = [p.rstrip("/") or "/" for p in paths if p != "/" and os.path.ismount(p)]
        if not mounts:
            return []
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            self.fd = libc.fanotify_init(FAN_CLOEXEC | FAN_CLASS_NOTIF, os.O_RDONLY | O_LARGEFILE | os.O_CLOEXEC)
        except (OSError, AttributeError):
            return []
        if self.fd < 0:
            return []
        for mp in mounts:
            if libc.fanotify_mark(self.fd, FAN_MARK_ADD | FAN_MARK_MOUNT, ctypes.c_uint64(FAN_CLOSE_WRITE),
                                  AT_FDCWD, mp.encode()) == 0:
                self.covered.append(mp)
            else:
                log.warning("fanotify no pudo vigilar %s (errno %d): se usará inotify", mp, ctypes.get_errno())
        if not self.covered:
            os.close(self.fd)
            self.fd = -1
            return []
        threading.Thread(target=self._events, daemon=True, name="fanotify-escritura").start()
        return list(self.covered)

    def _inside(self, path: str) -> bool:
        return any(path == m or path.startswith(m + "/") for m in self.covered)

    def _events(self) -> None:
        while not self._stop.is_set():
            try:
                buf = os.read(self.fd, 8192)
            except InterruptedError:
                continue
            except OSError:
                return
            off = 0
            while off + META.size <= len(buf):
                ev_len, _v, _r, _m, _mask, efd, _pid = META.unpack_from(buf, off)
                off += ev_len or META.size
                if efd < 0:
                    continue                            # cola desbordada: no hay archivo
                try:
                    path = os.readlink(f"/proc/self/fd/{efd}")
                except OSError:
                    path = ""
                finally:
                    os.close(efd)
                if path.endswith(" (deleted)"):
                    path = path[:-10]
                if path and self._inside(path):
                    try:
                        self.on_path(path)
                    except Exception:  # noqa: BLE001 — un fallo nunca para la vigilancia
                        log.exception("Error encolando %s", path)

    def stop(self) -> None:
        self._stop.set()
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


class RealTimeProtection:
    def __init__(self, scanner: Scanner, quarantine: Quarantine, auto_quarantine: bool = True, guard=None):
        self.guard = guard            # RansomwareGuard: ve cada modificación antes del escaneo
        self.scanner = scanner
        self.quarantine = quarantine
        self.auto_quarantine = auto_quarantine
        self._stop = threading.Event()
        self._wake = threading.Event()        # hay archivos pendientes de analizar
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()

    def on_detection(self, d: Detection) -> None:
        log.warning("AMENAZA DETECTADA: %s", d)
        if self.auto_quarantine and d.severity in ("high", "medium"):
            try:
                qid = self.quarantine.add(d.path, d.name, expected=d.ident)
                log.warning("Movido a cuarentena (%s): %r", qid, d.path)
            except OSError as e:
                log.error("No se pudo poner en cuarentena %r: %s", d.path, e)

    def check(self, path: str) -> None:
        if not os.path.lexists(path) and path.endswith(TEMP_SUFFIX):
            self._check_renamed(path)
            return
        if os.path.islink(path) or not os.path.isfile(path) or self.scanner._excluded(path):
            return
        try:
            dets = self.scanner.scan_file(path)
        except OSError:
            return
        if dets:
            self.on_detection(dets[0])
            for d in dets[1:]:
                log.warning("  + %s", d)

    def _check_renamed(self, path: str) -> None:
        """Una descarga terminada (X.crdownload → nombre final): fanotify avisó del temporal, que ya no existe;
        se analizan los archivos de esa carpeta modificados en los últimos segundos."""
        d = os.path.dirname(path)
        if self.scanner._excluded(d):
            return
        now = time.time()
        try:
            recent = sorted((e for e in os.scandir(d) if e.is_file(follow_symlinks=False)
                             and now - e.stat().st_mtime < 15), key=lambda e: -e.stat().st_mtime)[:10]
        except OSError:
            return
        for e in recent:
            if not e.path.endswith(TEMP_SUFFIX):
                self.check(e.path)

    def _enqueue(self, path: str) -> None:
        # Espera a que el archivo termine de escribirse (debounce).
        with self._lock:
            self._pending[path] = time.time()
        self._wake.set()

    def _worker(self) -> None:
        while not self._stop.is_set():
            # Sin archivos pendientes, el hilo duerme hasta que llegue uno (antes: despertaba cada 0,5 s siempre)
            if not self._pending:
                self._wake.wait(60)
                self._wake.clear()
                if self._stop.is_set():
                    break
            now = time.time()
            with self._lock:
                ready = [p for p, t in self._pending.items() if now - t > 1.0]
                for p in ready:
                    del self._pending[p]
            for p in ready:
                if self.guard is not None:
                    try:
                        self.guard.file_changed(p)
                    except Exception:  # noqa: BLE001 — el vigilante nunca debe parar el escáner
                        log.exception("Error en el detector de ransomware")
                self.check(p)
            self._stop.wait(0.5)

    def start(self, paths: list[str]) -> None:
        paths = [p for p in paths if os.path.isdir(p)]
        threading.Thread(target=self._worker, daemon=True).start()
        self._fan = FanotifyWatcher(self._enqueue) if os.geteuid() == 0 else None
        covered = self._fan.start(paths) if self._fan else []
        if covered:
            log.info("Vigilando con fanotify (una marca por montaje): %s", ", ".join(covered))
        paths = [p for p in paths if not any(p == c or p.startswith(c.rstrip("/") + "/") for c in covered)]
        if not paths:
            return
        try:
            from watchdog.events import FileSystemEventHandler  # type: ignore
            from watchdog.observers import Observer  # type: ignore
        except ImportError:
            log.info("watchdog no instalado: usando sondeo periódico")
            threading.Thread(target=self._poll, args=(paths,), daemon=True).start()
            return

        rtp = self

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    rtp._enqueue(event.src_path)

            def on_modified(self, event):
                if not event.is_directory:
                    rtp._enqueue(event.src_path)

            def on_moved(self, event):
                if not event.is_directory:
                    rtp._enqueue(event.dest_path)

        self._observer = Observer()
        for p in paths:
            self._observer.schedule(Handler(), p, recursive=True)
            log.info("Vigilando %s", p)
        self._observer.start()

    def _poll(self, paths: list[str]) -> None:
        mtimes: dict[str, float] = {}
        first = True
        while not self._stop.is_set():
            for root in paths:
                for f in self.scanner.iter_files(root):
                    try:
                        m = os.stat(f).st_mtime
                    except OSError:
                        continue
                    if mtimes.get(f) != m:
                        mtimes[f] = m
                        if not first:
                            self._enqueue(f)
            first = False
            # Solo si falta python3-watchdog (eventos inotify): recorrer carpetas despierta CPU y disco,
            # así que se hace poco (cada 2 min)
            self._stop.wait(120)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if getattr(self, "_fan", None):
            self._fan.stop()
        obs = getattr(self, "_observer", None)
        if obs:
            obs.stop()
