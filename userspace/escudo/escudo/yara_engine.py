"""Reglas YARA de la comunidad (YARA-Forge, signature-base…) como capa de firmas avanzada.

Las reglas viven en <data_dir>/yara/*.yar(a). ``escudo firmas yara-actualizar`` descarga el
paquete «core» de YARA-Forge (reglas curadas y probadas contra falsos positivos). Antes de
reemplazar las reglas vigentes se comprueba que compilan; si fallan, se conservan las anteriores.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

FORGE_URL = "https://github.com/YARAHQ/yara-forge/releases/latest/download/yara-forge-rules-core.zip"
MAX_RULES_ZIP = 200 * 1024 * 1024


def _sev(meta: dict) -> str:
    try:
        score = int(meta.get("score", 60))
    except (TypeError, ValueError):
        score = 60
    return "high" if score >= 80 else "medium" if score >= 60 else "low"


class YaraEngine:
    def __init__(self, rules_dir: Path):
        self.dir = rules_dir
        self.rules = None
        try:
            import yara
            self.yara = yara
        except ImportError:
            self.yara = None

    def _files(self) -> dict[str, str]:
        if not self.dir.is_dir():
            return {}
        return {f"r{i}": str(p) for i, p in enumerate(sorted(self.dir.rglob("*.yar")) + sorted(self.dir.rglob("*.yara")))}

    def load(self) -> int:
        files = self._files()
        if not self.yara or not files:
            return 0
        self.rules = self.yara.compile(filepaths=files)
        return len(files)

    def available(self) -> bool:
        if self.rules is None and self.yara:
            try:
                self.load()
            except Exception:  # noqa: BLE001 (yara.SyntaxError…)
                self.rules = None
        return self.rules is not None

    def match(self, path: str) -> list[dict]:
        if not self.available():
            return []
        try:
            hits = self.rules.match(path, timeout=15)
        except Exception:  # noqa: BLE001 (yara.TimeoutError / Error)
            return []
        return [{"rule": h.rule, "namespace": h.namespace, "tags": list(h.tags),
                 "severity": _sev(h.meta), "description": h.meta.get("description", "")} for h in hits]

    def update(self, url: str = FORGE_URL) -> int:
        with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 (https fijo)
            data = r.read(MAX_RULES_ZIP + 1)
        if len(data) > MAX_RULES_ZIP:
            raise ValueError("paquete de reglas demasiado grande")
        staging = Path(tempfile.mkdtemp(prefix=".yara-", dir=self.dir.parent))
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for m in z.infolist():
                    name = Path(m.filename).name           # sin rutas: evita zip-slip
                    if m.is_dir() or not name.endswith((".yar", ".yara")):
                        continue
                    (staging / name).write_bytes(z.read(m))
            if self.yara:                                  # debe compilar antes de reemplazar
                self.yara.compile(filepaths={f"r{i}": str(p) for i, p in enumerate(sorted(staging.glob("*.yar*")))})
            old = self.dir.with_name(self.dir.name + ".anterior")
            if old.exists():
                shutil.rmtree(old)
            if self.dir.exists():
                os.replace(self.dir, old)
            os.replace(staging, self.dir)
            self.rules = None
            return len(list(self.dir.glob("*.yar*")))
        finally:
            if staging.exists():
                shutil.rmtree(staging)
