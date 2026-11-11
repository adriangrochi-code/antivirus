"""Análisis del arranque con systemd-analyze: qué etapa y qué servicios lo retrasan."""

from __future__ import annotations

import re
import shutil
import subprocess

# Lo que SÍ debe estar en la fase 1 (ver el escritorio con la red y la seguridad activas)
ESSENTIAL = {"systemd-udevd", "systemd-journald", "nftables", "apparmor", "auditd", "dbus", "dbus-broker",
             "NetworkManager", "sddm", "dnscrypt-proxy", "systemd-logind", "polkit", "systemd-tmpfiles-setup",
             "systemd-fsck-root", "systemd-remount-fs", "systemd-sysctl", "systemd-modules-load",
             "centinela-firstboot", "plymouth-start", "systemd-random-seed"}
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


def recommendations(stages: dict, blame: list[tuple[float, str]]) -> list[str]:
    r = []
    if stages.get("firmware", 0) > 5:
        r.append(f"El firmware (BIOS/UEFI) tarda {stages['firmware']:.1f} s: activa «Fast Boot» y desactiva el "
                 "arranque por red y los controladores que no uses en la BIOS.")
    if stages.get("loader", 0) > 1.5:
        r.append(f"El gestor de arranque tarda {stages['loader']:.1f} s: con GRUB oculto (TISMA) debería ser < 1 s; "
                 "revisa el tiempo de espera del menú.")
    if stages.get("kernel", 0) > 3:
        r.append(f"El kernel tarda {stages['kernel']:.1f} s: compila el kernel a medida (centinela build-kernel) para "
                 "integrar los drivers del disco y no cargar módulos al arrancar.")
    if stages.get("initrd", 0) > 2:
        r.append(f"El initramfs tarda {stages['initrd']:.1f} s: centinela apply deja solo los módulos de este equipo.")
    for t, unit in blame[:15]:
        name = unit.rsplit(".", 1)[0]
        if t >= 1.0 and name not in ESSENTIAL and not unit.endswith((".device", ".mount", ".target")):
            r.append(f"{unit} tarda {t:.1f} s: diferirlo a la fase 2 (después del escritorio) o desactivarlo si no se usa.")
    return r


def analyze() -> dict:
    if not shutil.which("systemd-analyze"):
        raise RuntimeError("systemd-analyze no está disponible")
    run = lambda *a: subprocess.run(["systemd-analyze", *a], capture_output=True, text=True, timeout=60).stdout  # noqa: E731
    stages = parse_time(run("time"))
    blame = parse_blame(run("blame"))
    return {"etapas": stages, "lentos": blame[:15], "cadena": run("critical-chain", "graphical.target"),
            "recomendaciones": recommendations(stages, blame)}
