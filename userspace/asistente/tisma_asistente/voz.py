"""Voz 100 % local: Piper (neuronal, natural) si hay una voz instalada; si no, espeak-ng.

Cada frase se sintetiza una vez y se guarda en caché (WAV), así la bienvenida suena al instante.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "tisma-asistente"
VOICES = [DATA / "voces", Path("/usr/share/tisma/voces")]
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tisma-asistente"


def piper_voice() -> Path | None:
    for d in VOICES:
        voices = sorted(d.glob("es_*.onnx")) + sorted(d.glob("*.onnx")) if d.is_dir() else []
        if voices:
            return voices[0]
    return None


def engine() -> str:
    if shutil.which("piper") and piper_voice():
        return "piper"
    if shutil.which("espeak-ng"):
        return "espeak-ng"
    return "ninguno"


def synthesize(text: str) -> Path | None:
    eng = engine()
    if eng == "ninguno":
        return None
    key = hashlib.sha1(f"{eng}|{piper_voice()}|{text}".encode()).hexdigest()[:16]
    out = CACHE / f"{key}.wav"
    if out.exists():
        return out
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.wav")
    if eng == "piper":
        r = subprocess.run(["piper", "--model", str(piper_voice()), "--output_file", str(tmp)],
                           input=text, text=True, capture_output=True, timeout=120)
    else:
        r = subprocess.run(["espeak-ng", "-v", "es", "-s", "160", "-w", str(tmp), text],
                           capture_output=True, timeout=60)
    if r.returncode != 0 or not tmp.exists():
        return None
    tmp.rename(out)
    return out


def play(wav: Path) -> bool:
    for player in (["pw-play"], ["paplay"], ["aplay", "-q"]):
        if shutil.which(player[0]):
            return subprocess.run([*player, str(wav)], capture_output=True).returncode == 0
    return False


def say(text: str) -> bool:
    wav = synthesize(text)
    return bool(wav) and play(wav)
