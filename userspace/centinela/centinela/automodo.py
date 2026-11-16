"""Modo automático (opcional, desactivado por defecto; documento de traspaso §4.3):
* a batería y con 20 % o menos → Eco (se recuerda el modo que había);
* al enchufar → vuelve el modo que había antes de bajar a Eco.
Si la persona elige otro modo a mano, manda ella (se olvida el recordado). Lo revisa adaptd cada ~minuto."""
from __future__ import annotations

from . import sysfs

UMBRAL = 20


def capacidad() -> int | None:
    vals = [sysfs.read_int(f"{b}/capacity") for b in sysfs.ls("/sys/class/power_supply/BAT*")]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def decidir(prefs: dict, en_ac: bool, cap: int | None) -> tuple[str | None, dict]:
    """(perfil al que pasar o None, preferencias actualizadas)."""
    prefs = dict(prefs)
    if not prefs.get("auto_modo"):
        return None, prefs
    actual = prefs.get("perfil", "equilibrado")
    previo = prefs.get("auto_previo")
    if not en_ac and cap is not None and cap <= UMBRAL and actual != "silencioso" and not previo:
        prefs["auto_previo"] = actual
        prefs["perfil"] = "silencioso"
        return "silencioso", prefs
    if en_ac and previo:
        prefs.pop("auto_previo")
        prefs["perfil"] = previo
        return (previo if previo != actual else None), prefs
    return None, prefs
