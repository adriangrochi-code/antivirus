"""Trazas del agente para entrenar a Hope (solo si el usuario lo activa).

Cada paso con herramienta queda como una línea JSONL con el esquema acordado con Hope:
{tarea, herramienta, entrada, salida, ok, modelo_generador, licencia_modelo, repo_licencia}.

- Desactivado por defecto: se activa con «trazas_hope»: true en ~/.config/tisma-code/config.json.
- Se guarda SOLO en este equipo (~/.local/share/tisma-code/trazas-hope, permisos 600). Nada se envía: son los
  archivos y el código del usuario, y él decide si pasa el archivo a Hope.
- Hope solo entrena con trazas de modelos cuya licencia lo permite: por eso cada línea dice qué modelo la
  generó y con qué licencia («desconocida» si no se sabe; Hope las descarta).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import models

DIR = models.MODELS_DIR.parent / "trazas-hope"
MAX_CAMPO = 20000

_LICENCIAS = (("apache-2.0", re.compile(r"Apache License,?\s+Version 2\.0", re.I)),
              ("mit", re.compile(r"Permission is hereby granted, free of charge", re.I)),
              ("bsd-3-clause", re.compile(r"Neither the name of .{0,120} nor the names", re.I | re.S)),
              ("bsd-2-clause", re.compile(r"Redistribution and use in source and binary forms", re.I)),
              ("mpl-2.0", re.compile(r"Mozilla Public License,?\s+(?:v\.|Version)\s*2\.0", re.I)),
              ("agpl-3.0", re.compile(r"GNU AFFERO GENERAL PUBLIC LICENSE", re.I)),
              ("lgpl", re.compile(r"GNU LESSER GENERAL PUBLIC LICENSE", re.I)),
              ("gpl", re.compile(r"GNU GENERAL PUBLIC LICENSE", re.I)),
              ("unlicense", re.compile(r"This is free and unencumbered software", re.I)))


def licencia_repo(raiz: str | Path) -> str:
    """Licencia del proyecto según su archivo LICENSE/COPYING (o «desconocida»)."""
    for nombre in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "LICENCE"):
        p = Path(raiz) / nombre
        try:
            texto = p.read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            continue
        for spdx, rx in _LICENCIAS:
            if rx.search(texto):
                return spdx
        return "otra"
    return "desconocida"


def licencia_modelo(modelo: str, base: Path | None = None) -> str:
    """La licencia guardada al descargar el modelo (licencia.json en su carpeta) o «desconocida»."""
    base = base or models.MODELS_DIR
    if str(modelo).lower().startswith("hope"):
        return "hope"
    for f in base.glob("*/licencia.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if any(Path(a).name == Path(modelo).name for a in d.get("archivos", [])) or \
                f.parent.name.replace("__", "/") == modelo:
            return str(d.get("licencia") or "desconocida")[:64]
    return "desconocida"


def _clip(v) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= MAX_CAMPO else s[:MAX_CAMPO] + "…[recortado]"


class Trazas:
    def __init__(self, raiz: str | Path, dir: Path | None = None):
        self.dir = dir or DIR
        self.repo = licencia_repo(raiz)
        self._lic: dict[str, str] = {}

    def archivo(self) -> Path:
        return self.dir / time.strftime("%Y-%m-%d.jsonl")

    def paso(self, tarea: str, herramienta: str, entrada, salida: str, ok: bool, modelo: str) -> None:
        if modelo not in self._lic:
            self._lic[modelo] = licencia_modelo(modelo)
        linea = {"tarea": _clip(tarea), "herramienta": herramienta, "entrada": _clip(entrada),
                 "salida": _clip(salida), "ok": bool(ok), "modelo_generador": modelo,
                 "licencia_modelo": self._lic[modelo], "repo_licencia": self.repo}
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.archivo(), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(linea, ensure_ascii=False) + "\n")
