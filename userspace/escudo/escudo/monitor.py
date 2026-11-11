"""Protección en tiempo real: escanea archivos nuevos o modificados."""

from __future__ import annotations

import logging
import os
import threading
import time

from .quarantine import Quarantine
from .scanner import Detection, Scanner

log = logging.getLogger("escudo.monitor")


class RealTimeProtection:
    def __init__(self, scanner: Scanner, quarantine: Quarantine, auto_quarantine: bool = True):
        self.scanner = scanner
        self.quarantine = quarantine
        self.auto_quarantine = auto_quarantine
        self._stop = threading.Event()
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()

    def on_detection(self, d: Detection) -> None:
        log.warning("AMENAZA DETECTADA: %s", d)
        if self.auto_quarantine and d.severity in ("high", "medium"):
            try:
                qid = self.quarantine.add(d.path, d.name)
                log.warning("Movido a cuarentena (%s): %s", qid, d.path)
            except OSError as e:
                log.error("No se pudo poner en cuarentena %s: %s", d.path, e)

    def check(self, path: str) -> None:
        if not os.path.isfile(path) or self.scanner._excluded(path):
            return
        try:
            dets = self.scanner.scan_file(path)
        except OSError:
            return
        if dets:
            self.on_detection(dets[0])
            for d in dets[1:]:
                log.warning("  + %s", d)

    def _enqueue(self, path: str) -> None:
        # Espera a que el archivo termine de escribirse (debounce).
        with self._lock:
            self._pending[path] = time.time()

    def _worker(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                ready = [p for p, t in self._pending.items() if now - t > 1.0]
                for p in ready:
                    del self._pending[p]
            for p in ready:
                self.check(p)
            self._stop.wait(0.5)

    def start(self, paths: list[str]) -> None:
        paths = [p for p in paths if os.path.isdir(p)]
        threading.Thread(target=self._worker, daemon=True).start()
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
            self._stop.wait(5)

    def stop(self) -> None:
        self._stop.set()
        obs = getattr(self, "_observer", None)
        if obs:
            obs.stop()
