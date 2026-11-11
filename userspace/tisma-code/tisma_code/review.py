"""Calidad por verificación, adaptado de Hope (runtime/loop.py, runtime/budget.py, council/coordinator.py).

* ``Router``: varios modelos locales (p. ej. uno veloz y uno potente). Elige con UCB1 según el
  historial del proyecto (Auer et al. 2002) y **escala** al siguiente nivel cuando la
  verificación falla. Ningún modelo se aprueba a sí mismo: decide ``verify``.
* ``ReviewLoop``: intenta → verifica → reintenta con el fallo como crítica. Se detiene si
  queda verificado, si no hay progreso (mismo fallo dos veces) o si se acaba el presupuesto.
* ``anytime_budget``: con una velocidad objetivo, si el modelo termina antes de tiempo, el
  sobrante se usa para que revise su propio diff.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

PASS_RE = re.compile(r"\[código de salida 0\]")


@dataclass
class ModelSpec:
    name: str
    endpoint: str
    model: str
    tier: int = 0          # 0 = veloz, 1 = potente, ...


@dataclass
class Router:
    models: list[ModelSpec]
    stats: dict[str, tuple[int, int]] = field(default_factory=dict)   # nombre -> (intentos, aciertos)

    def first(self) -> ModelSpec:
        """UCB1 sobre los modelos del nivel más bajo (los más rápidos)."""
        low = min(m.tier for m in self.models)
        cands = [m for m in self.models if m.tier == low]
        total = sum(self.stats.get(m.name, (0, 0))[0] for m in cands) or 1
        def ucb(m: ModelSpec) -> float:
            t, w = self.stats.get(m.name, (0, 0))
            return float("inf") if t == 0 else w / t + math.sqrt(2 * math.log(total) / t)
        return max(cands, key=ucb)

    def escalate(self, current: ModelSpec) -> ModelSpec | None:
        higher = sorted((m for m in self.models if m.tier > current.tier), key=lambda m: m.tier)
        return higher[0] if higher else None


def verify_passed(output: str) -> bool:
    blocks = [b for b in output.split("\n\n$ ") if b.strip()]
    return bool(blocks) and all(PASS_RE.search(b) for b in blocks)


def failure_signature(output: str) -> str:
    # Sin números (tiempos, líneas) para reconocer «el mismo fallo» aunque cambie la duración
    return hashlib.sha1(re.sub(r"\d+", "#", output[-3000:]).encode()).hexdigest()


@dataclass
class ReviewLoop:
    max_rounds: int = 3
    seen: set[str] = field(default_factory=set)
    rounds: int = 0
    failures: list[str] = field(default_factory=list)

    def next(self, verify_output: str) -> str:
        """'verificado' | 'reintentar' | 'sin_progreso' | 'presupuesto'"""
        if verify_passed(verify_output):
            return "verificado"
        self.rounds += 1
        sig = failure_signature(verify_output)
        self.failures.append(verify_output)
        if sig in self.seen:
            return "sin_progreso"
        self.seen.add(sig)
        return "reintentar" if self.rounds <= self.max_rounds else "presupuesto"


def critique_message(verify_output: str, round_no: int, escalated_to: str | None) -> str:
    extra = f" (ahora te atiende el modelo {escalated_to})" if escalated_to else ""
    return (f"Verificación automática, ronda {round_no}{extra}: las comprobaciones del proyecto fallan.\n"
            f"{verify_output[-3500:]}\n\nAnaliza la causa raíz (no solo el síntoma), corrígela y vuelve "
            "a llamar a verify. No desactives ni borres pruebas para que pasen.")


def anytime_budget(completion_tokens: int, elapsed_s: float, target_tps: float) -> float:
    """Segundos sobrantes respecto de la velocidad objetivo (0 si no hay objetivo)."""
    if target_tps <= 0 or completion_tokens <= 0:
        return 0.0
    return max(0.0, completion_tokens / target_tps - elapsed_s)


SELF_REVIEW = ("Te sobra tiempo: revisa el diff de tus cambios buscando errores (casos borde, "
               "nombres, imports, manejo de errores, seguridad). Si está bien responde exactamente "
               "«REVISADO OK». Si no, corrígelo con las herramientas.\n\n{diff}")
