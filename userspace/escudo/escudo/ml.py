"""Detección por aprendizaje automático con los modelos abiertos de EMBER2024 (Apache-2.0).

EMBER2024 (FutureComputing4AI) publica clasificadores LightGBM entrenados con 3,2 millones de
archivos: ejecutables de Windows (PE), Linux (ELF), Android (APK) y PDF. Detectan malware
*nuevo* a partir de características estáticas (cabeceras, imports, entropía, cadenas…): el archivo
nunca se ejecuta.

Los modelos se descargan de Hugging Face fijando revisión y SHA-256 (cadena de suministro).
Un modelo de ML da falsos positivos: por sí solo genera una alerta «baja» (sin cuarentena).
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REVISION = "e5b945dd90e1a1a1ec0cc07b3a17b52e9ba2d0c2"
URL = "https://huggingface.co/joyce8/EMBER2024-benchmark-models/resolve/{rev}/{file}"
MODELS = {
    "PE": ("EMBER2024_PE.model", "4252027863492ac138785c8c18576f43dad77d00faddc14e8c0072e8db419f99"),
    "ELF": ("EMBER2024_ELF.model", "8e4072938955ac6020f80bca6cff7c12e97865102d578b3099e2011048eb9c43"),
    "PDF": ("EMBER2024_PDF.model", "a7e30eb883f8918cf28aaa98b6124cf2eb25a98ea3d58f3053a4fb49e24c3161"),
    "APK": ("EMBER2024_APK.model", "a13274463201e7d3ddec74b098350051266fcf0812aff302617397f1765cbc90"),
}
THRESHOLD_SUSPICIOUS = 0.90
THRESHOLD_HIGH = 0.99
MAX_BYTES = 30 * 1024 * 1024


def file_kind(data: bytes) -> str | None:
    if data[:2] == b"MZ":
        return "PE"
    if data[:4] == b"\x7fELF":
        return "ELF"
    if data[:5] == b"%PDF-":
        return "PDF"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "AndroidManifest.xml" in z.namelist():
                    return "APK"
        except zipfile.BadZipFile:
            return None
    return None


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_models(model_dir: Path, kinds=("PE", "ELF", "PDF", "APK")) -> list[str]:
    """Descarga y verifica los modelos. Un archivo con hash distinto se descarta."""
    model_dir.mkdir(parents=True, exist_ok=True)
    done = []
    for kind in kinds:
        name, sha = MODELS[kind]
        dest = model_dir / name
        if dest.exists() and _sha256(dest) == sha:
            done.append(name)
            continue
        fd, tmp = tempfile.mkstemp(dir=model_dir, prefix=".descarga-")
        try:
            with os.fdopen(fd, "wb") as out, urllib.request.urlopen(  # noqa: S310 (https fijo)
                    URL.format(rev=REVISION, file=name), timeout=300) as r:
                while chunk := r.read(1 << 20):
                    out.write(chunk)
            if _sha256(Path(tmp)) != sha:
                raise ValueError(f"{name}: SHA-256 incorrecto, descarga descartada")
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
            done.append(name)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return done


class MLEngine:
    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self._boosters: dict[str, object] = {}
        self._extractor = None
        try:
            import lightgbm  # noqa: F401
            from thrember.features import PEFeatureExtractor  # noqa: F401
            self.libs_ok = True
        except ImportError:
            self.libs_ok = False

    def available(self) -> bool:
        return self.libs_ok and any((self.model_dir / n).exists() for n, _ in MODELS.values())

    def _booster(self, kind: str):
        if kind not in self._boosters:
            import lightgbm as lgb
            name, sha = MODELS[kind]
            p = self.model_dir / name
            if not p.exists() or _sha256(p) != sha:     # nunca cargar un modelo manipulado
                self._boosters[kind] = None
            else:
                self._boosters[kind] = lgb.Booster(model_file=str(p))
        return self._boosters[kind]

    def vector(self, data: bytes):
        import numpy as np
        if self._extractor is None:
            from thrember.features import PEFeatureExtractor
            self._extractor = PEFeatureExtractor()
        return np.array(self._extractor.feature_vector(data), dtype=np.float32)

    def score(self, data: bytes) -> tuple[str, float] | None:
        """(tipo, probabilidad de malware 0..1), o None si no aplica."""
        if not self.libs_ok or len(data) > MAX_BYTES:
            return None
        kind = file_kind(data)
        if not kind:
            return None
        booster = self._booster(kind)
        if booster is None:
            return None
        return kind, float(booster.predict([self.vector(data)])[0])
