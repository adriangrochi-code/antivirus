"""Cuarentena segura frente a carreras con enlaces simbólicos (Escudo corre como root).

Ataque que evita (auditoría A4/M1): un usuario hace que Escudo detecte su archivo y, mientras se
escanea, cambia una carpeta de la ruta por un enlace a /home/victima/.ssh; si la cuarentena volviera
a resolver la ruta, root borraría (o al restaurar, escribiría) archivos de otro usuario.

Defensa:
* La ruta se recorre componente a componente con ``openat(O_NOFOLLOW|O_DIRECTORY)``: ningún enlace
  se sigue jamás. El archivo se abre con ``O_NOFOLLOW`` y se lee y borra **desde ese mismo
  descriptor/directorio**, comprobando dispositivo+inodo (lo escaneado es lo que se aísla).
* Al restaurar se crea el archivo con ``O_CREAT|O_EXCL|O_NOFOLLOW`` en un directorio abierto del
  mismo modo, y se devuelve a su dueño original (uid/gid).
* El contenido se guarda comprimido (gzip): no ejecutable ni detectable por otros motores, y rápido.
* Cuota total y por archivo: un usuario no puede llenar el disco del sistema.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path

from .config import IS_WINDOWS

_QID = re.compile(r"^[0-9a-f]{32}$")
MAX_FILE = 200 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRFLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class QuarantineError(OSError):
    pass


def secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        os.chmod(path, 0o700)


def open_parent_nofollow(path: str, create: bool = False) -> tuple[int, str]:
    """Abre el directorio padre de ``path`` sin seguir ningún enlace. Devuelve (dirfd, nombre)."""
    p = os.path.normpath(path)
    if not os.path.isabs(p):
        raise QuarantineError(f"ruta no absoluta: {path}")
    parts = [x for x in p.split(os.sep) if x]
    if not parts:
        raise QuarantineError("ruta vacía")
    fd = os.open("/", DIRFLAGS)
    try:
        for comp in parts[:-1]:
            if comp in (".", ".."):
                raise QuarantineError("ruta con componentes relativos")
            try:
                nfd = os.open(comp, DIRFLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(comp, 0o755, dir_fd=fd)
                nfd = os.open(comp, DIRFLAGS, dir_fd=fd)
            except OSError as e:
                raise QuarantineError(f"componente no seguro en la ruta ({comp}): {e}") from e
            os.close(fd)
            fd = nfd
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def open_file_nofollow(path: str) -> tuple[int, int, str]:
    """(dirfd, filefd, nombre) de un archivo regular, sin seguir enlaces en ningún punto."""
    dfd, name = open_parent_nofollow(path)
    try:
        ffd = os.open(name, os.O_RDONLY | NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
    except OSError:
        os.close(dfd)
        raise
    if not stat.S_ISREG(os.fstat(ffd).st_mode):
        os.close(ffd)
        os.close(dfd)
        raise QuarantineError("no es un archivo regular")
    return dfd, ffd, name


class Quarantine:
    def __init__(self, directory: Path, max_total: int = MAX_TOTAL):
        self.dir = directory
        self.max_total = max_total
        secure_dir(self.dir)

    def _meta(self, qid: str) -> Path:
        if not _QID.match(qid):
            raise ValueError(f"ID de cuarentena inválido: {qid}")
        return self.dir / f"{qid}.json"

    def used(self) -> int:
        return sum(p.stat().st_size for p in self.dir.glob("*.bin"))

    def add(self, path: str, threat: str, expected: tuple[int, int] | None = None) -> str:
        """Aísla ``path``. ``expected`` = (st_dev, st_ino) de lo que se escaneó: si el archivo cambió
        por otro (enlace, renombrado), no se toca nada."""
        dfd, ffd, name = open_file_nofollow(os.path.abspath(path))
        try:
            st = os.fstat(ffd)
            if expected and (st.st_dev, st.st_ino) != expected:
                raise QuarantineError("el archivo cambió desde el escaneo: no se toca")
            if st.st_size > MAX_FILE or self.used() + st.st_size > self.max_total:
                raise QuarantineError("cuota de cuarentena superada")
            qid = uuid.uuid4().hex
            blob = self.dir / f"{qid}.bin"
            fd = os.open(blob, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as gz:
                with os.fdopen(os.dup(ffd), "rb") as src:
                    while chunk := src.read(1 << 20):
                        gz.write(chunk)
            # Borrar exactamente ese inodo desde el directorio ya abierto
            now = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
                blob.unlink()
                raise QuarantineError("el archivo fue sustituido durante la cuarentena: no se toca")
            os.unlink(name, dir_fd=dfd)
            meta = {"id": qid, "original_path": os.path.abspath(path), "threat": threat, "size": st.st_size,
                    "mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid, "gid": st.st_gid,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")}
            mfd = os.open(self._meta(qid), os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
            with os.fdopen(mfd, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=1)
            return qid
        finally:
            os.close(ffd)
            os.close(dfd)

    def list(self) -> list[dict]:
        return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(self.dir.glob("*.json"))]

    def restore(self, qid: str, dest: str | None = None) -> str:
        meta = json.loads(self._meta(qid).read_text(encoding="utf-8"))
        target = os.path.abspath(dest or meta["original_path"])
        dfd, name = open_parent_nofollow(target)
        try:
            # Se restaura sin bit de ejecución y nunca sobre algo existente ni a través de enlaces
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
                         meta.get("mode", 0o644) & 0o666, dir_fd=dfd)
            with os.fdopen(fd, "wb") as out, gzip.open(self.dir / f"{qid}.bin", "rb") as gz:
                while chunk := gz.read(1 << 20):
                    out.write(chunk)
                if os.geteuid() == 0 and "uid" in meta:
                    os.fchown(out.fileno(), meta["uid"], meta["gid"])
        except FileExistsError as e:
            raise FileExistsError(f"Ya existe: {target}") from e
        finally:
            os.close(dfd)
        self.delete(qid)
        return target

    def delete(self, qid: str) -> None:
        self._meta(qid)  # valida el ID
        for suffix in (".bin", ".json"):
            p = self.dir / f"{qid}{suffix}"
            if p.exists():
                p.unlink()

    def purge(self) -> int:
        items = list(self.dir.glob("*.json"))
        for m in items:
            self.delete(m.stem)
        return len(items)
