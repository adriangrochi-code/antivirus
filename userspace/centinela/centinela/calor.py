"""Calor: detectar cuando el procesador baja su velocidad para no recalentarse (ventilador sucio, pasta seca).

Un portátil con años de polvo puede rendir la mitad sin que nadie sepa por qué. adaptd llama a ``Calor.tick``
cada pocos segundos (solo lee /sys y /proc, nada más): cuenta el tiempo con el procesador ocupado y, de ese
tiempo, cuánto estuvo caliente Y frenado a la vez. Se guarda un resumen por día; ``salud-hardware semanal`` avisa
en simple si pasa a menudo. No cambia nada del sistema.
"""
from __future__ import annotations

import json
import os
import time

from . import sysfs
from .apply import STATE_DIR

ARCHIVO = os.path.join(STATE_DIR, "calor.json")
SENSORES = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "acpitz")
DIAS = 14
GUARDAR_CADA_S = 300


def temperatura_cpu() -> float | None:
    """°C del procesador (el primer sensor conocido)."""
    por_nombre = {}
    for h in sysfs.ls("/sys/class/hwmon/hwmon*"):
        n = sysfs.read(f"{h}/name")
        if n in SENSORES and n not in por_nombre:
            por_nombre[n] = h
    for n in SENSORES:
        if n in por_nombre:
            v = sysfs.read_int(f"{por_nombre[n]}/temp1_input")
            if v:
                return v / 1000
    return None


def frecuencia_relativa() -> float | None:
    """Frecuencia media actual / máxima de fábrica (0-1)."""
    r = []
    for c in sysfs.ls("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"):
        cur, mx = sysfs.read_int(f"{c}/scaling_cur_freq"), sysfs.read_int(f"{c}/cpuinfo_max_freq")
        if cur and mx:
            r.append(cur / mx)
    return sum(r) / len(r) if r else None


def tiempos_cpu() -> tuple[int, int] | None:
    """(ocupado, total) en ticks desde el arranque, de /proc/stat."""
    linea = (sysfs.read("/proc/stat", "") or "").split("\n", 1)[0].split()
    if not linea or linea[0] != "cpu":
        return None
    v = [int(x) for x in linea[1:]]
    libre = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v) - libre, sum(v)


def frenado(temp: float | None, frec: float | None, ocupado: float) -> bool:
    """Caliente y lento con trabajo pendiente: el procesador se está frenando por temperatura."""
    return temp is not None and frec is not None and ocupado >= 0.6 and temp >= 85 and frec < 0.7


class Calor:
    def __init__(self, archivo: str = ARCHIVO, reloj=time.time):
        self.archivo, self.reloj = archivo, reloj
        self._prev = None
        self._t = None
        self._guardado = reloj()
        self.dias: dict[str, dict] = self._leer()

    def _leer(self) -> dict:
        try:
            with open(self.archivo) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def tick(self, temp=None, frec=None, cpu=None) -> bool:
        ahora = self.reloj()
        cpu = cpu if cpu is not None else tiempos_cpu()
        if cpu is None:
            return False
        prev, t_prev = self._prev, self._t
        self._prev, self._t = cpu, ahora
        if prev is None or cpu[1] <= prev[1]:
            return False
        dt = min(ahora - t_prev, 30)              # tras una suspensión no se cuenta el hueco
        ocupado = (cpu[0] - prev[0]) / (cpu[1] - prev[1])
        if ocupado < 0.6:
            return False
        temp = temp if temp is not None else temperatura_cpu()
        frec = frec if frec is not None else frecuencia_relativa()
        f = frenado(temp, frec, ocupado)
        dia = self.dias.setdefault(time.strftime("%Y-%m-%d", time.localtime(ahora)),
                                   {"ocupado_s": 0, "frenado_s": 0, "max_c": 0})
        dia["ocupado_s"] += round(dt)
        if f:
            dia["frenado_s"] += round(dt)
        if temp:
            dia["max_c"] = max(dia["max_c"], round(temp))
        if ahora - self._guardado >= GUARDAR_CADA_S:
            self.guardar()
        return f

    def guardar(self) -> None:
        self._guardado = self.reloj()
        for viejo in sorted(self.dias)[:-DIAS]:
            del self.dias[viejo]
        os.makedirs(os.path.dirname(self.archivo), exist_ok=True)
        tmp = self.archivo + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.dias, f)
        os.replace(tmp, self.archivo)


def resumen(dias: dict, ultimos: int = 7) -> dict:
    """{ocupado_min, frenado_min, proporcion, max_c} de los últimos días."""
    sel = [dias[k] for k in sorted(dias)[-ultimos:]]
    oc = sum(d.get("ocupado_s", 0) for d in sel)
    fr = sum(d.get("frenado_s", 0) for d in sel)
    return {"ocupado_min": oc // 60, "frenado_min": fr // 60, "proporcion": fr / oc if oc else 0.0,
            "max_c": max((d.get("max_c", 0) for d in sel), default=0)}


def se_frena_seguido(r: dict) -> bool:
    """Al menos 10 min en la semana y una quinta parte del tiempo de trabajo: no es un pico aislado."""
    return r["frenado_min"] >= 10 and r["proporcion"] >= 0.2
