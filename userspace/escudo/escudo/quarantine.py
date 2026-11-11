"""Cuarentena: aísla archivos maliciosos de forma que no puedan ejecutarse.

El contenido se cifra con XOR (clave aleatoria por archivo) para que ni el
sistema ni otros programas puedan ejecutarlo o abrirlo accidentalmente, y se
guardan metadatos para poder restaurarlo.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import time
import uuid
from pathlib import Path

from .config import IS_WINDOWS


_QID = re.compile(r"^[0-9a-f]{32}$")


def _xor(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    k = (key * (len(data) // len(key) + 1))[: len(data)]
    n = int.from_bytes(data, "big") ^ int.from_bytes(k, "big")
    return n.to_bytes(len(data), "big")


def secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        os.chmod(path, 0o700)


class Quarantine:
    def __init__(self, directory: Path):
        self.dir = directory
        secure_dir(self.dir)

    def _meta(self, qid: str) -> Path:
        if not _QID.match(qid):
            raise ValueError(f"ID de cuarentena inválido: {qid}")
        return self.dir / f"{qid}.json"

    def add(self, path: str, threat: str) -> str:
        src = Path(path).resolve()
        data = src.read_bytes()
        key = secrets.token_bytes(32)
        qid = uuid.uuid4().hex
        blob = self.dir / f"{qid}.bin"
        blob.write_bytes(_xor(data, key))
        if not IS_WINDOWS:
            os.chmod(blob, 0o600)
        st = src.stat()
        meta = {
            "id": qid,
            "original_path": str(src),
            "threat": threat,
            "size": len(data),
            "mode": stat.S_IMODE(st.st_mode),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "key": key.hex(),
        }
        self._meta(qid).write_text(json.dumps(meta, indent=1), encoding="utf-8")
        if not IS_WINDOWS:
            os.chmod(self._meta(qid), 0o600)
        src.unlink()
        return qid

    def list(self) -> list[dict]:
        items = []
        for f in sorted(self.dir.glob("*.json")):
            m = json.loads(f.read_text(encoding="utf-8"))
            m.pop("key", None)
            items.append(m)
        return items

    def restore(self, qid: str, dest: str | None = None) -> str:
        meta = json.loads(self._meta(qid).read_text(encoding="utf-8"))
        blob = self.dir / f"{qid}.bin"
        target = Path(dest or meta["original_path"])
        if target.exists():
            raise FileExistsError(f"Ya existe: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_xor(blob.read_bytes(), bytes.fromhex(meta["key"])))
        if not IS_WINDOWS:
            # Se restaura sin bit de ejecución por seguridad.
            os.chmod(target, meta.get("mode", 0o644) & ~0o111)
        self.delete(qid)
        return str(target)

    def delete(self, qid: str) -> None:
        self._meta(qid)  # valida el ID
        for suffix in (".bin", ".json"):
            p = self.dir / f"{qid}{suffix}"
            if p.exists():
                p.unlink()

    def purge(self) -> int:
        n = len(list(self.dir.glob("*.json")))
        shutil.rmtree(self.dir)
        secure_dir(self.dir)
        return n
