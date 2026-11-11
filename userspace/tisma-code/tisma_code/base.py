"""Piezas compartidas por tools y devtools (evita importaciones circulares)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

MAX_OUTPUT = 20_000


class ToolError(Exception):
    pass


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n… [{len(text) - limit} caracteres omitidos] …\n" + text[-limit // 2:]


def safe_dir(root: Path, *parts: str) -> Path:
    """Crea (si falta) root/parts… comprobando CADA componente: ni enlaces simbólicos ni nada que no sea un
    directorio. Un repositorio malicioso podría traer `.tisma/rea -> ~` y, montado en una jaula de bubblewrap,
    expondría la carpeta personal con escritura."""
    cur = Path(root)
    os.makedirs(cur, exist_ok=True)
    st = os.stat(cur)
    if not stat.S_ISDIR(st.st_mode):
        raise ToolError(f"{cur} no es un directorio")
    for part in parts:
        if not part or part in (".", "..") or "/" in part:
            raise ToolError(f"componente de ruta inválido: {part!r}")
        cur = cur / part
        try:
            os.mkdir(cur, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(cur)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ToolError(f"{cur} es un enlace simbólico o no es una carpeta: por seguridad no se usa")
    return cur
