"""Protección en tiempo real: escanea archivos nuevos o modificados."""

from __future__ import annotations

import errno
import logging
import os
import threading
import time

from . import fan
from .quarantine import Quarantine
from .scanner import Detection, Scanner

log = logging.getLogger("escudo.monitor")

FAN_CLOEXEC, FAN_CLASS_NOTIF = 0x1, 0x0
FAN_CLOSE_WRITE = 0x8
O_LARGEFILE = 0o100000
# Descargas del navegador y guardados atómicos: se escriben con un nombre temporal y luego se renombran
TEMP_SUFFIX = (".crdownload", ".part", ".download", ".partial", ".tmp")


class FanotifyWatcher:
    """Archivos cerrados tras escribirse bajo unas carpetas, sin una vigilancia por carpeta.

    inotify (watchdog) necesita una vigilancia por CADA carpeta: con un /home grande (node_modules, cachés,
    proyectos) son decenas de miles, cada una retiene su inodo en la memoria del kernel, y al arrancar hay que
    recorrer todo el árbol. fanotify marca el sistema de archivos entero (ver fan.py: también ve lo escrito desde
    sandboxes y otros espacios de montaje) y cada evento se filtra por dispositivo y ruta.
    """

    def __init__(self, on_event):
        self.on_event = on_event                       # (ruta, inodo)
        self.fd = -1
        self.covered: list[str] = []
        self.devs: set[int] = set()
        self._stop = threading.Event()
        self._overflow_log = 0.0

    def start(self, paths: list[str]) -> list[str]:
        """Vigila las rutas que puede; devuelve cuáles (el resto queda para inotify)."""
        try:
            lib = fan.libc()
            self.fd = lib.fanotify_init(FAN_CLOEXEC | FAN_CLASS_NOTIF, os.O_RDONLY | O_LARGEFILE | os.O_CLOEXEC)
        except (OSError, AttributeError) as e:
            log.warning("fanotify no disponible (%s): se usará inotify", e)
            return []
        if self.fd < 0:
            import ctypes
            log.warning("fanotify_init falló (errno %d; ¿falta CAP_SYS_ADMIN en el servicio?): se usará inotify",
                        ctypes.get_errno())
            return []
        roots = [p.rstrip("/") or "/" for p in paths if p != "/"]
        for r in roots + fan.submounts(roots):
            if fan.mark(lib, self.fd, r, FAN_CLOSE_WRITE) and r in roots:
                self.covered.append(r)
        if not self.covered:
            os.close(self.fd)
            self.fd = -1
            return []
        self.devs = fan.devices(self.covered + fan.submounts(self.covered))
        self._desp = fan.Despertador()
        self._hilo = threading.Thread(target=self._events, daemon=True, name="fanotify-escritura")
        self._hilo.start()
        return list(self.covered)

    def _events(self) -> None:
        while not self._stop.is_set():
            if not fan.esperar(self.fd, self._desp):
                return
            try:
                buf = os.read(self.fd, 16384)
            except InterruptedError:
                continue
            except OSError as e:
                if e.errno == errno.EBADF or self._stop.is_set():
                    return
                # Sin FAN_REPORT_FD_ERROR, un fallo al abrir el archivo del evento (EMFILE, EPERM de un LSM…)
                # llega aquí: antes terminaba el hilo y la protección quedaba apagada sin avisar
                log.warning("Error leyendo eventos de fanotify (%s): se sigue", e)
                time.sleep(0.2)
                continue
            off = 0
            while off + fan.META.size <= len(buf):
                ev_len, _v, _r, _m, mask, efd, _pid = fan.META.unpack_from(buf, off)
                off += ev_len or fan.META.size
                if mask & fan.FAN_Q_OVERFLOW:
                    if time.monotonic() - self._overflow_log > 60:
                        self._overflow_log = time.monotonic()
                        log.warning("Cola de fanotify desbordada: algunas escrituras no se analizaron en tiempo real")
                if efd < 0:
                    continue
                try:
                    st = os.fstat(efd)
                    if st.st_dev not in self.devs:
                        continue                        # otro subvolumen/disco: descartado sin leer la ruta
                    path = fan.fd_path(efd, st)
                except OSError:
                    continue
                finally:
                    os.close(efd)
                if fan.inside(path, self.covered):
                    try:
                        self.on_event(path, st.st_ino)
                    except Exception:  # noqa: BLE001 — un fallo nunca para la vigilancia
                        log.exception("Error encolando %s", path)

    def stop(self) -> None:
        self._stop.set()
        hilo = getattr(self, "_hilo", None)
        self.fd = fan.parar_hilos([hilo] if hilo else [], self.fd, getattr(self, "_desp", None), log)


class RealTimeProtection:
    def __init__(self, scanner: Scanner, quarantine: Quarantine, auto_quarantine: bool = True, guard=None):
        self.guard = guard            # RansomwareGuard: ve cada modificación antes del escaneo
        self.scanner = scanner
        self.quarantine = quarantine
        self.auto_quarantine = auto_quarantine
        self._stop = threading.Event()
        self._wake = threading.Event()        # hay archivos pendientes de analizar
        self._pending: dict[str, float] = {}
        self._inodes: dict[str, int] = {}     # ruta → inodo al cerrarse (para encontrarlo si se renombra)
        self._lock = threading.Lock()

    def on_detection(self, d: Detection) -> None:
        log.warning("AMENAZA DETECTADA: %s", d)
        if self.auto_quarantine and d.severity in ("high", "medium"):
            try:
                qid = self.quarantine.add(d.path, d.name, expected=d.ident)
                log.warning("Movido a cuarentena (%s): %r", qid, d.path)
            except OSError as e:
                log.error("No se pudo poner en cuarentena %r: %s", d.path, e)

    def check(self, path: str, ino: int | None = None) -> None:
        if not os.path.lexists(path):
            self._check_renamed(path, ino)
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

    def _check_renamed(self, path: str, ino: int | None = None) -> None:
        """El archivo ya no está con ese nombre: se cerró y se renombró (descargas X.crdownload → final, guardados
        atómicos, o malware que escribe y renombra para esquivar el análisis). Se busca por su INODO en la misma
        carpeta (un renombrado lo conserva); si no se sabe, los archivos de la carpeta modificados hace poco."""
        d = os.path.dirname(path)
        if self.scanner._excluded(d):
            return
        try:
            entries = [e for e in os.scandir(d) if e.is_file(follow_symlinks=False)]
        except OSError:
            return
        if ino is not None:
            for e in entries:
                if e.inode() == ino:
                    self.check(e.path)
                    return
        if not path.endswith(TEMP_SUFFIX):
            return
        now = time.time()
        recent = sorted((e for e in entries if now - e.stat().st_mtime < 15), key=lambda e: -e.stat().st_mtime)[:10]
        for e in recent:
            if not e.path.endswith(TEMP_SUFFIX):
                self.check(e.path)

    def _enqueue(self, path: str, ino: int | None = None) -> None:
        # Espera a que el archivo termine de escribirse (debounce).
        with self._lock:
            self._pending[path] = time.time()
            if ino is not None:
                self._inodes[path] = ino
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
                inodes = {}
                for p in ready:
                    del self._pending[p]
                    inodes[p] = self._inodes.pop(p, None)
            for p in ready:
                if self.guard is not None:
                    try:
                        self.guard.file_changed(p)
                    except Exception:  # noqa: BLE001 — el vigilante nunca debe parar el escáner
                        log.exception("Error en el detector de ransomware")
                self.check(p, inodes.get(p))
            self._stop.wait(0.5)

    def start(self, paths: list[str]) -> None:
        paths = [p for p in paths if os.path.isdir(p)]
        threading.Thread(target=self._worker, daemon=True).start()
        self._fan = FanotifyWatcher(self._enqueue) if os.geteuid() == 0 else None
        covered = self._fan.start(paths) if self._fan else []
        if covered:
            log.info("Vigilando con fanotify (marca por sistema de archivos): %s", ", ".join(covered))
        paths = [p for p in paths if p not in covered]
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
