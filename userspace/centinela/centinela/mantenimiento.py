"""Mantenimiento con el equipo libre: las tareas pesadas esperan a que el equipo esté enchufado y sin uso.

Se usa como ExecStartPre de las tareas programadas pesadas (análisis de rootkits, auditoría, scrub, copias a
disco externo, comprobación de drivers…): en vez de ejecutarse a batería o en mitad del trabajo, o de
SALTARSE (como hace ConditionACPower, que en una tarea mensual la deja un mes sin hacer), ESPERAN.

* «Libre»: enchufado (o sin batería), carga baja durante dos comprobaciones seguidas y la IA local sin generar
  (Modo IA).
* Límite: tras ESPERA_MAX se ejecuta igual (siempre con prioridad mínima de CPU y disco), salvo con la batería
  baja; tras ESPERA_ABS se desiste hasta la próxima vez. Así un portátil que nunca se enchufa no se queda sin
  análisis de seguridad.
"""

from __future__ import annotations

import os
import time
from typing import Callable

from . import modo_ia, sysfs

ESPERA_MAX = 12 * 3600        # s: pasado esto, se ejecuta aunque no esté libre (si la batería lo permite)
ESPERA_ABS = 24 * 3600        # s: pasado esto, se desiste (la próxima ejecución del temporizador lo intentará)
BATERIA_MIN = 30              # % por debajo del cual, a batería, nunca se fuerza
PASO = 60                     # s entre comprobaciones


def energia() -> tuple[bool, int | None]:
    """(enchufado, % de batería). Sin batería (sobremesa) cuenta como enchufado."""
    enchufado, hay_bateria, pct = False, False, None
    for ps in sysfs.ls("/sys/class/power_supply/*"):
        tipo = sysfs.read(f"{ps}/type") or ""
        if tipo == "Mains" and sysfs.read(f"{ps}/online") == "1":
            enchufado = True
        elif tipo == "Battery":
            hay_bateria = True
            cap = sysfs.read(f"{ps}/capacity")
            if cap and cap.isdigit():
                pct = int(cap) if pct is None else min(pct, int(cap))
    return (enchufado or not hay_bateria), pct


def carga_baja(load: Callable[[], float] = lambda: os.getloadavg()[0]) -> bool:
    return load() < max(0.6, (os.cpu_count() or 1) * 0.3)


def libre(energia_f=energia, carga_f=carga_baja, ia_f=modo_ia.activo) -> tuple[bool, str]:
    enchufado, _ = energia_f()
    if not enchufado:
        return False, "a batería"
    if ia_f():
        return False, "la IA local está generando"
    if not carga_f():
        return False, "equipo en uso"
    return True, "libre"


def esperar(max_s: float = ESPERA_MAX, abs_s: float = ESPERA_ABS, paso: float = PASO,
            libre_f=libre, energia_f=energia, now=time.monotonic, sleep=time.sleep,
            log: Callable[[str], None] = print) -> int:
    """0 = adelante (libre, o se esperó demasiado y hay batería); 1 = desistir esta vez."""
    t0 = now()
    seguidas = 0
    ultimo = ""
    while True:
        ok, motivo = libre_f()
        seguidas = seguidas + 1 if ok else 0
        if seguidas >= 2:                       # dos comprobaciones seguidas: no es un instante de calma
            return 0
        espera = now() - t0
        if espera >= max_s:
            enchufado, pct = energia_f()
            if enchufado or pct is None or pct >= BATERIA_MIN:
                log(f"Llevaba {espera / 3600:.0f} h esperando ({motivo}): se ejecuta igual, con prioridad mínima")
                return 0
            if espera >= abs_s:
                log(f"Batería baja ({pct} %) tras {espera / 3600:.0f} h: se intentará la próxima vez")
                return 1
        if motivo != ultimo and not ok:
            log(f"Esperando a que el equipo esté libre: {motivo}")
            ultimo = motivo
        sleep(paso if not ok else min(paso, 30))
