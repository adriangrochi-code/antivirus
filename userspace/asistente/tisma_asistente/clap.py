"""Detección de doble palmada (implementación propia).

Una palmada es un pico brusco de energía respecto del ruido de fondo. El suelo de ruido
se adapta lentamente y solo con bloques tranquilos, así una conversación o música no
lo arrastran. Dos picos separados entre MIN_GAP y MAX_GAP segundos = doble palmada.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class ClapConfig:
    spike_ratio: float = 7.0      # cuántas veces por encima del ruido debe estar el pico
    min_rms: float = 0.012        # pico mínimo absoluto (habitaciones muy silenciosas)
    min_gap_s: float = 0.08       # dos palmadas más juntas que esto son el mismo golpe
    max_gap_s: float = 0.45
    cooldown_s: float = 1.5       # tras disparar, ignora palmadas un rato
    floor_alpha: float = 0.99     # inercia del suelo de ruido
    quiet_mult: float = 2.5       # el suelo solo aprende de bloques por debajo de suelo·esto


@dataclass
class ClapDetector:
    cfg: ClapConfig = field(default_factory=ClapConfig)
    floor: float = 0.002
    _last_clap: float = -math.inf
    _last_fire: float = -math.inf
    _in_spike: bool = False

    @staticmethod
    def rms(samples) -> float:
        n = len(samples)
        return math.sqrt(sum(float(x) * float(x) for x in samples) / n) if n else 0.0

    def feed(self, level: float, t: float) -> bool:
        """Procesa la energía (RMS) de un bloque en el instante t. True = doble palmada."""
        c = self.cfg
        spike = level >= max(c.min_rms, self.floor * c.spike_ratio)
        if not spike and level < self.floor * c.quiet_mult:
            self.floor = c.floor_alpha * self.floor + (1 - c.floor_alpha) * level
        if not spike:
            self._in_spike = False
            return False
        if self._in_spike:                       # mismo golpe que el bloque anterior
            return False
        self._in_spike = True
        if t - self._last_fire < c.cooldown_s:
            return False
        gap = t - self._last_clap
        if c.min_gap_s <= gap <= c.max_gap_s:
            self._last_fire, self._last_clap = t, -math.inf
            return True
        if gap >= c.min_gap_s:
            self._last_clap = t
        return False


def listen(on_double_clap, device=None, sample_rate: int = 44100, block_ms: int = 40,
           cfg: ClapConfig | None = None, stop=None) -> None:
    """Escucha el micrófono (sounddevice) y llama a on_double_clap() en cada doble palmada."""
    import time

    import numpy as np
    import sounddevice as sd

    det = ClapDetector(cfg or ClapConfig())
    block = int(sample_rate * block_ms / 1000)
    with sd.InputStream(device=device, channels=1, samplerate=sample_rate, blocksize=block, dtype="float32") as s:
        while not (stop and stop.is_set()):
            data, _ = s.read(block)
            level = float(np.sqrt(np.mean(np.square(data))))
            if det.feed(level, time.monotonic()):
                on_double_clap()


def pick_input_device(probe_s: float = 0.5, silent_rms: float = 0.001):
    """Micrófono por defecto; si está mudo, el que más señal capta."""
    import numpy as np
    import sounddevice as sd

    def level(dev):
        try:
            rec = sd.rec(int(16000 * probe_s), samplerate=16000, channels=1, device=dev, dtype="float32")
            sd.wait()
            return float(np.sqrt(np.mean(np.square(rec))))
        except Exception:  # noqa: BLE001
            return -1.0

    default = sd.default.device[0]
    if default is not None and default >= 0 and level(default) > silent_rms:
        return default
    inputs = [i for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    best = max(inputs, key=level, default=None)
    return best
