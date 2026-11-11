"""Piezas compartidas por tools y devtools (evita importaciones circulares)."""

from __future__ import annotations

MAX_OUTPUT = 20_000


class ToolError(Exception):
    pass


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n… [{len(text) - limit} caracteres omitidos] …\n" + text[-limit // 2:]
