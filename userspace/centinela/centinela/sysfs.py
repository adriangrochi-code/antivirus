"""Lectura segura de /sys y /proc. ``ROOT`` permite tests con un árbol falso."""

from __future__ import annotations

import glob
import os

ROOT = os.environ.get("CENTINELA_SYSROOT", "/")


def path(p: str) -> str:
    return os.path.join(ROOT, p.lstrip("/"))


def read(p: str, default: str | None = None) -> str | None:
    try:
        with open(path(p), encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


def read_int(p: str, default: int | None = None) -> int | None:
    v = read(p)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def ls(pattern: str) -> list[str]:
    """Glob relativo a ROOT; devuelve rutas *sin* el prefijo ROOT."""
    root = ROOT.rstrip("/")
    return sorted(p[len(root):] if root else p for p in glob.glob(path(pattern)))
