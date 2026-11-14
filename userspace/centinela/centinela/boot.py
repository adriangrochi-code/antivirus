"""Análisis del arranque con systemd-analyze: qué etapa y qué servicios lo retrasan."""

from __future__ import annotations

import re
import shutil
import subprocess

# Lo que SÍ debe estar en la fase 1 (ver el escritorio con la red y la seguridad activas)
ESSENTIAL = {"systemd-udevd", "systemd-journald", "nftables", "apparmor", "auditd", "dbus", "dbus-broker",
             "NetworkManager", "sddm", "dnscrypt-proxy", "systemd-logind", "polkit", "systemd-tmpfiles-setup",
             "systemd-fsck-root", "systemd-remount-fs", "systemd-sysctl", "systemd-modules-load",
             "centinela-firstboot", "plymouth-start", "systemd-random-seed",
             # Seguridad antes de iniciar sesión, a propósito: nunca se recomienda retrasarla
             "escudo", "usbguard", "usbguard-dbus", "tisma-bloqueo-sesion", "tisma-montajes-seguros"}
_T = re.compile(r"(?:(\d+)min )?(?:([\d.]+)s|(\d+)ms)")


def seconds(txt: str) -> float:
    total = 0.0
    for m, s, ms in re.findall(r"(?:(\d+)min\s*)?(?:([\d.]+)s\b|(\d+)ms)", txt):
        total += int(m or 0) * 60 + float(s or 0) + int(ms or 0) / 1000
    return total


def parse_time(out: str) -> dict:
    stages = {}
    for val, name in re.findall(r"([\d.]+m?s|\d+min [\d.]+s) \((firmware|loader|kernel|initrd|userspace)\)", out):
        stages[name] = seconds(val)
    m = re.search(r"graphical\.target reached after (.+?) in userspace", out)
    if m:
        stages["escritorio"] = seconds(m.group(1))
    return stages


def parse_blame(out: str) -> list[tuple[float, str]]:
    res = []
    for line in out.splitlines():
        m = re.match(r"\s*(.+?)\s+(\S+\.(?:service|mount|device|socket|target|timer))$", line)
        if m:
            res.append((seconds(m.group(1)), m.group(2)))
    return res


ETAPAS = {"firmware": "El equipo antes del sistema (BIOS)", "loader": "Menú de arranque",
          "kernel": "Núcleo del sistema", "initrd": "Abrir el disco (incluye escribir la contraseña)",
          "userspace": "Servicios del sistema", "escritorio": "Hasta la pantalla de inicio de sesión",
          "sesion": "Entrar al escritorio (después de tu contraseña)"}
NOMBRES = {"escudo": "Escudo (antivirus)", "NetworkManager": "la red", "plymouth-quit-wait": "la pantalla de carga",
           "ModemManager": "los módems móviles", "accounts-daemon": "las cuentas de usuario",
           "udisks2": "los discos y memorias USB", "fwupd": "la revisión de firmware", "cups": "las impresoras",
           "bluetooth": "el Bluetooth", "rasdaemon": "el registro de errores de memoria",
           "plasma-plasmashell": "el escritorio", "plasma-kwin_wayland": "el gestor de ventanas",
           "xdg-desktop-portal": "los permisos de las apps", "plasma-kcminit": "la configuración inicial del escritorio",
           "kde-baloo": "el buscador de archivos", "plasma-ksplash": "la pantalla de carga"}


def amigable(unit: str) -> str:
    nombre = unit.rsplit(".", 1)[0]
    return NOMBRES.get(nombre, nombre)


def recommendations(stages: dict, blame: list[tuple[float, str]], sesion: list[tuple[float, str]] = ()) -> list[str]:
    """Qué retrasa el arranque, en simple y sin órdenes de terminal."""
    r = []
    if stages.get("firmware", 0) > 5:
        r.append(f"El equipo tarda {stages['firmware']:.1f} s antes de empezar el sistema: en la BIOS/UEFI activa "
                 "«Fast Boot» y quita el arranque por red si no lo usas.")
    if stages.get("loader", 0) > 1.5:
        r.append(f"El menú de arranque tarda {stages['loader']:.1f} s (debería ser menos de 1 s).")
    if stages.get("kernel", 0) > 3:
        r.append(f"El núcleo tarda {stages['kernel']:.1f} s: el kernel TISMA a medida de este equipo arranca más "
                 "rápido (TISMA Rendimiento > Optimizar).")
    for t, unit in blame[:15]:
        name = unit.rsplit(".", 1)[0]
        if t >= 1.0 and name not in ESSENTIAL and not unit.endswith((".device", ".mount", ".target")):
            r.append(f"Al encender: {amigable(unit)}, {t:.1f} s ({unit}). Se puede dejar para después de que "
                     "aparezca el escritorio.")
    for t, unit in list(sesion)[:8]:
        if t >= 2.0 and not unit.endswith((".target", ".slice", ".socket")):
            r.append(f"Al entrar: {amigable(unit)}, {t:.1f} s ({unit}).")
    return r


def informe_texto(d: dict) -> str:
    """Todo junto para copiar y enviar (soporte)."""
    lineas = ["TISMA - análisis del arranque"]
    lineas += [f"{ETAPAS.get(k, k)}: {v:.1f} s" for k, v in d.get("etapas", {}).items()]
    lineas += ["", "Servicios más lentos al encender:"] + [f"  {t:6.1f} s  {u}" for t, u in d.get("lentos", [])]
    lineas += ["", "Al entrar al escritorio:"] + [f"  {t:6.1f} s  {u}" for t, u in d.get("sesion", [])]
    lineas += ["", "Cadena crítica:", d.get("cadena", "")]
    return "\n".join(lineas)


def tiempo_sesion(run=None) -> float | None:
    """Segundos desde que arranca la sesión del usuario (tras escribir la contraseña) hasta que el escritorio de
    Plasma está listo (plasma-plasmashell activo)."""
    run = run or (lambda *a: subprocess.run(["systemctl", "--user", "show", *a], capture_output=True, text=True,
                                            timeout=10).stdout)
    def num(txt: str, clave: str) -> int:
        m = re.search(rf"^{clave}=(\d+)$", txt, re.M)
        return int(m.group(1)) if m else 0
    inicio = num(run("-p", "UserspaceTimestampMonotonic"), "UserspaceTimestampMonotonic")
    listo = num(run("plasma-plasmashell.service", "-p", "ActiveEnterTimestampMonotonic"), "ActiveEnterTimestampMonotonic")
    return round((listo - inicio) / 1e6, 1) if inicio and listo > inicio else None


def analyze() -> dict:
    if not shutil.which("systemd-analyze"):
        raise RuntimeError("systemd-analyze no está disponible")
    run = lambda *a: subprocess.run(["systemd-analyze", *a], capture_output=True, text=True, timeout=60).stdout  # noqa: E731
    stages = parse_time(run("time"))
    blame = parse_blame(run("blame"))
    sesion = parse_blame(run("--user", "blame"))
    t_sesion = tiempo_sesion()
    if t_sesion:
        stages["sesion"] = t_sesion
    d = {"etapas": stages, "nombres": ETAPAS, "lentos": blame[:15], "sesion": sesion[:10],
         "cadena": run("critical-chain", "graphical.target"), "recomendaciones": recommendations(stages, blame, sesion)}
    d["texto"] = informe_texto(d)
    return d
