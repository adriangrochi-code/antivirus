"""Energía adaptativa: con batería, cada componente a su modo de bajo consumo; con cargador, a pleno rendimiento.

Lo que con batería se cambia (y con cargador se deshace), al conectar o desconectar el cargador:
* SATA: gestión de energía del enlace (med_power_with_dipm): el disco entra en reposo entre accesos.
* PCIe: ASPM «powersupersave» y gestión de energía en tiempo de ejecución de los dispositivos PCI (auto).
* USB: suspensión automática de lo que no se usa, EXCEPTO teclados, ratones y mandos (clase HID), que con
  ella pierden pulsaciones o tardan en despertar.
* Audio: el códec HDA se apaga tras 1 s de silencio.
* Escritura a disco agrupada cada 15 s en vez de 5 s (el disco despierta menos).
* Estadísticas de memoria del kernel cada 10 s (menos despertares de todas las CPU).
Todo a través de la lista blanca de centinela (Actuator): nada fuera de estas rutas.

Umbrales de carga (opcional, portátiles con soporte: ThinkPad, muchos ASUS/Dell/Lenovo): cargar solo hasta
el 80 % alarga varios años la vida de la batería si el equipo pasa mucho tiempo enchufado.
"""

from __future__ import annotations

from . import sysfs
from .apply import Actuator, load_state, save_state

BATTERY = {"sata": "med_power_with_dipm", "aspm": "powersupersave", "runtime_pm": "auto", "usb": "auto",
           "audio": "1", "dirty_writeback": "1500", "stat_interval": "10"}
AC = {"sata": "max_performance", "aspm": "default", "runtime_pm": "on", "usb": "on",
      "audio": "0", "dirty_writeback": "500", "stat_interval": "1"}


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


def _is_hid(dev: str) -> bool:
    """¿Algún interfaz del dispositivo USB es HID (teclado, ratón, mando) o es un concentrador?"""
    if sysfs.read(f"{dev}/bDeviceClass") == "09":                 # hub
        return True
    return any(sysfs.read(f"{i}/bInterfaceClass") == "03" for i in sysfs.ls(f"{dev}/*:*"))


def apply(mode: str, act: Actuator) -> dict:
    v = BATTERY if mode == "bateria" else AC
    for h in sysfs.ls("/sys/class/scsi_host/host*"):
        if sysfs.read(f"{h}/link_power_management_policy") is not None:
            act.write(f"{h}/link_power_management_policy", v["sata"])
    if sysfs.read("/sys/module/pcie_aspm/parameters/policy") is not None:
        act.write("/sys/module/pcie_aspm/parameters/policy", v["aspm"])
    for d in sysfs.ls("/sys/bus/pci/devices/*"):
        if sysfs.read(f"{d}/power/control") is not None:
            act.write(f"{d}/power/control", v["runtime_pm"])
    for d in sysfs.ls("/sys/bus/usb/devices/*"):
        if sysfs.read(f"{d}/power/control") is not None and sysfs.read(f"{d}/idVendor") and not _is_hid(d):
            act.write(f"{d}/power/control", v["usb"])
    if sysfs.read("/sys/module/snd_hda_intel/parameters/power_save") is not None:
        act.write("/sys/module/snd_hda_intel/parameters/power_save", v["audio"])
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
