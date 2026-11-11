"""Energía adaptativa: con batería se ahorra SOLO en lo que no se nota al usar el equipo.

Criterio: el portátil se usa con normalidad con batería. Nada que el usuario perciba (USB, audio, red) cambia;
solo ajustes internos invisibles. Con batería (y al revés con cargador), al conectar o desconectar el cargador:
* SATA: gestión de energía del enlace (med_power_with_dipm): el disco entra en reposo entre accesos.
* PCIe: ASPM «powersave» (moderado) y gestión de energía en tiempo de ejecución de los dispositivos PCI,
  EXCEPTO red y controladoras USB (sin cortes de Wi-Fi ni retrasos al conectar algo).
* Escritura a disco agrupada cada 15 s en vez de 5 s (el disco despierta menos).
* Estadísticas de memoria del kernel cada 10 s (menos despertares de todas las CPU).
No se tocan: los puertos y dispositivos USB (funcionan siempre igual) ni el audio (sin clics al volver a sonar).
Todo a través de la lista blanca de centinela (Actuator): nada fuera de estas rutas.

Umbrales de carga (opcional, portátiles con soporte: ThinkPad, muchos ASUS/Dell/Lenovo): cargar solo hasta
el 80 % alarga varios años la vida de la batería si el equipo pasa mucho tiempo enchufado.
"""

from __future__ import annotations

from . import sysfs
from .apply import Actuator, load_state, save_state

BATTERY = {"sata": "med_power_with_dipm", "aspm": "powersave", "runtime_pm": "auto",
           "dirty_writeback": "1500", "stat_interval": "10"}
AC = {"sata": "max_performance", "aspm": "default", "runtime_pm": "on", "dirty_writeback": "500", "stat_interval": "1"}
# Clases PCI que nunca se suspenden: red (0x02), controladoras USB (0x0c03)
PCI_KEEP = ("0x02", "0x0c03")


def on_ac() -> bool:
    """Con cargador (o equipo de sobremesa sin batería)."""
    supplies = sysfs.ls("/sys/class/power_supply/*")
    batteries = [s for s in supplies if sysfs.read(f"{s}/type") == "Battery"]
    if not batteries:
        return True
    mains = [s for s in supplies if sysfs.read(f"{s}/type") in ("Mains", "USB")]
    if any(sysfs.read(f"{m}/online") == "1" for m in mains):
        return True
    return not any(sysfs.read(f"{b}/status") == "Discharging" for b in batteries) and bool(mains)


def apply(mode: str, act: Actuator) -> dict:
    v = BATTERY if mode == "bateria" else AC
    for h in sysfs.ls("/sys/class/scsi_host/host*"):
        if sysfs.read(f"{h}/link_power_management_policy") is not None:
            act.write(f"{h}/link_power_management_policy", v["sata"])
    if sysfs.read("/sys/module/pcie_aspm/parameters/policy") is not None:
        act.write("/sys/module/pcie_aspm/parameters/policy", v["aspm"])
    for d in sysfs.ls("/sys/bus/pci/devices/*"):
        cls = sysfs.read(f"{d}/class") or ""
        if sysfs.read(f"{d}/power/control") is not None and not cls.startswith(PCI_KEEP):
            act.write(f"{d}/power/control", v["runtime_pm"])
    act.sysctl("vm.dirty_writeback_centisecs", v["dirty_writeback"])
    act.sysctl("vm.stat_interval", v["stat_interval"])
    apply_thresholds(act)
    return {"modo": mode, "cambios": len(act.changes)}


def apply_thresholds(act: Actuator) -> None:
    """Umbrales de carga guardados por el usuario (prefs.json → umbral_carga)."""
    end = (load_state("prefs.json") or {}).get("umbral_carga")
    if not end:
        return
    for b in sysfs.ls("/sys/class/power_supply/BAT*"):
        if sysfs.read(f"{b}/charge_control_end_threshold") is not None:
            # El orden importa: algunos firmwares rechazan un inicio mayor o igual que el final
            act.write(f"{b}/charge_control_end_threshold", str(end))
            if sysfs.read(f"{b}/charge_control_start_threshold") is not None:
                act.write(f"{b}/charge_control_start_threshold", str(max(40, int(end) - 5)))


def set_threshold(end: int | None, act: Actuator) -> str:
    prefs = load_state("prefs.json") or {}
    if end is None or end >= 100:
        prefs.pop("umbral_carga", None)
        save_state("prefs.json", prefs)
        for b in sysfs.ls("/sys/class/power_supply/BAT*"):
            if sysfs.read(f"{b}/charge_control_end_threshold") is not None:
                act.write(f"{b}/charge_control_end_threshold", "100")
        return "Carga completa (100 %)."
    end = max(50, min(int(end), 99))
    if not any(sysfs.read(f"{b}/charge_control_end_threshold") is not None
               for b in sysfs.ls("/sys/class/power_supply/BAT*")):
        return "Este portátil no permite limitar la carga desde el sistema."
    prefs["umbral_carga"] = end
    save_state("prefs.json", prefs)
    apply_thresholds(act)
    return f"La batería cargará hasta el {end} % (alarga su vida si el equipo pasa mucho tiempo enchufado)."
