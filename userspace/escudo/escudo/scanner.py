"""Motor de escaneo: firmas (hash + patrones) y heurística."""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .config import Config
from .signatures import SignatureDB

CHUNK = 1024 * 1024
# Solapamiento entre bloques para no perder patrones que crucen el límite.
OVERLAP = 4096

EXECUTABLE_EXT = {".exe", ".dll", ".scr", ".com", ".pif", ".bat", ".cmd", ".vbs",
                  ".js", ".jse", ".wsf", ".hta", ".ps1", ".msi", ".lnk", ".jar", ".sh", ".elf"}
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".jpg",
           ".jpeg", ".png", ".gif", ".mp3", ".mp4", ".zip", ".rar"}
# Macros que se ejecutan automáticamente en documentos Office antiguos.
_AUTO_MACRO = re.compile(rb"(?i)(AutoOpen|Document_Open|Workbook_Open|Auto_Open)")
_SHELL_MACRO = re.compile(rb"(?i)(WScript\.Shell|Shell\s*\(|CreateObject\s*\(\s*\"?MSXML2\.XMLHTTP)")


@dataclass
class Detection:
    path: str
    name: str
    severity: str
    method: str  # hash | pattern | heuristic

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.path}: {self.name} ({self.method})"


@dataclass
class ScanReport:
    scanned: int = 0
    skipped: int = 0
    errors: int = 0
    detections: list[Detection] = field(default_factory=list)


def shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in Counter(data).values())


class Scanner:
    def __init__(self, config: Config, db: SignatureDB | None = None):
        self.config = config
        self.db = db or SignatureDB(config.signatures_file)
        self.max_size = config.max_file_size_mb * 1024 * 1024
        self.excludes = [os.path.normcase(os.path.abspath(p)) for p in config.exclude_dirs]
        self.excludes.append(os.path.normcase(os.path.abspath(config.quarantine_dir)))

    def _excluded(self, path: str) -> bool:
        p = os.path.normcase(os.path.abspath(path))
        return any(p == e or p.startswith(e.rstrip(os.sep) + os.sep) for e in self.excludes)

    def iter_files(self, root: str) -> Iterator[str]:
        if os.path.isfile(root):
            yield root
            return
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not self._excluded(os.path.join(dirpath, d))]
            for f in filenames:
                full = os.path.join(dirpath, f)
                if not os.path.islink(full):
                    yield full

    def scan_file(self, path: str) -> list[Detection]:
        try:
            st = os.stat(path)
        except OSError:
            raise
        if not os.path.isfile(path) or st.st_size > self.max_size:
            return []

        sha = hashlib.sha256()
        found: dict[str, Detection] = {}
        head = b""
        tail = b""
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                sha.update(chunk)
                window = tail + chunk
                if not head:
                    head = chunk[:CHUNK]
                for pat in self.db.patterns:
                    if pat.name not in found and pat.regex.search(window):
                        found[pat.name] = Detection(path, pat.name, pat.severity, "pattern")
                tail = chunk[-OVERLAP:]

        detections = list(found.values())
        name = self.db.lookup_hash(sha.hexdigest())
        if name:
            detections.insert(0, Detection(path, name, "high", "hash"))
        if self.config.heuristics and not detections:
            detections.extend(self._heuristics(path, head))
        return detections

    def _heuristics(self, path: str, head: bytes) -> list[Detection]:
        out: list[Detection] = []
        name = os.path.basename(path).lower()
        suffixes = Path(name).suffixes

        # Doble extensión engañosa: factura.pdf.exe
        if len(suffixes) >= 2 and suffixes[-1] in EXECUTABLE_EXT and suffixes[-2] in DOC_EXT:
            out.append(Detection(path, "Heur.DoubleExtension", "high", "heuristic"))
        # Carácter de control RTLO usado para ocultar la extensión real
        if "‮" in name:
            out.append(Detection(path, "Heur.RTLO.Filename", "high", "heuristic"))

        is_pe = head[:2] == b"MZ"
        is_elf = head[:4] == b"\x7fELF"
        # Ejecutable disfrazado con extensión de documento
        if (is_pe or is_elf) and suffixes and suffixes[-1] in DOC_EXT:
            out.append(Detection(path, "Heur.MasqueradedExecutable", "high", "heuristic"))
        # Ejecutable empaquetado/cifrado (entropía muy alta)
        if (is_pe or is_elf) and len(head) >= 64 * 1024:
            if shannon_entropy(head) >= self.config.entropy_threshold:
                out.append(Detection(path, "Heur.PackedExecutable", "low", "heuristic"))
            if b"UPX!" in head:
                out.append(Detection(path, "Heur.Packer.UPX", "low", "heuristic"))
        # Macros con auto-ejecución + ejecución de comandos (documentos OLE)
        if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            if _AUTO_MACRO.search(head) and _SHELL_MACRO.search(head):
                out.append(Detection(path, "Heur.Office.AutoExecMacro", "medium", "heuristic"))
        return out

    def scan(self, root: str, on_detection=None) -> ScanReport:
        report = ScanReport()
        for path in self.iter_files(root):
            try:
                if os.path.getsize(path) > self.max_size:
                    report.skipped += 1
                    continue
                dets = self.scan_file(path)
            except (OSError, PermissionError):
                report.errors += 1
                continue
            report.scanned += 1
            for d in dets:
                report.detections.append(d)
                if on_detection:
                    on_detection(d)
        return report
