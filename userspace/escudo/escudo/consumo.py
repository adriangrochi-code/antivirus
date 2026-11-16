"""Abuso de CPU: criptomineros y otros programas que usan el equipo a escondidas.

Señales (miradas cada 30 s, solo con /proc, sin auditd):
* Programa en una carpeta del usuario (Descargas, /tmp, ~/…) que usa al menos media CPU de forma SOSTENIDA:
  - con una conexión a un puerto típico de minería → aviso grave a los 2 minutos;
  - con un nombre que imita un proceso del kernel («kworker», «kthreadd»…) → aviso grave a los 2 minutos;
  - mientras la pantalla está bloqueada (el usuario no está) → aviso a los 10 minutos.
* Los programas del sistema (/usr), Wine, Flatpak y las herramientas de programación instaladas en la carpeta
  personal (rustup, cargo, nvm, conda…) no cuentan: compilar o jugar no es un ataque.

Solo AVISA (con el botón «Ver informe»); no detiene nada: cortar por error un programa del usuario sería peor.
"""
from __future__ import annotations

import os
import re
import subprocess

from .behavior import ZONAS_USUARIO, Alert

TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
UMBRAL_CPU = 0.5                       # núcleos (media CPU) sostenidos
SEG_GRAVE = 120
SEG_BLOQUEADO = 600
PUERTOS_MINERIA = {3333, 4444, 5555, 7777, 14444, 45700}
DISFRAZ = re.compile(r"^(kworker|kthreadd|ksoftirqd|kswapd|migration|rcu_|watchdog|systemd-|\[)")
HERRAMIENTAS = ("/.rustup/", "/.cargo/", "/.nvm/", "/.pyenv/", "/miniconda", "/anaconda", "/.conda/",
                "/.local/share/Steam/", "/steamapps/", "/.sdkman/", "/go/pkg/", "/.local/share/tisma-code/",
                "/.local/share/JetBrains/", "/.vscode", "/.local/share/flatpak/")


def sospechoso(exe: str) -> bool:
    return exe.startswith(ZONAS_USUARIO) and not any(h in exe for h in HERRAMIENTAS)


def muestra(proc: str = "/proc") -> dict[int, tuple[int, str, str]]:
    """{pid: (ticks de CPU, ejecutable, nombre)} de los procesos con programa en carpetas del usuario."""
    out = {}
    for d in os.listdir(proc):
        if not d.isdigit():
            continue
        try:
            exe = os.readlink(f"{proc}/{d}/exe")
            if exe.endswith(" (deleted)"):
                exe = exe[:-len(" (deleted)")]
            if not sospechoso(exe):
                continue
            stat = open(f"{proc}/{d}/stat").read()
            nombre = stat[stat.index("(") + 1:stat.rindex(")")]
            campos = stat[stat.rindex(")") + 2:].split()
            out[int(d)] = (int(campos[11]) + int(campos[12]), exe, nombre)
        except (OSError, ValueError, IndexError):
            continue
    return out


def conexion_minera(pid: int, proc: str = "/proc") -> str | None:
    """Destino «ip:puerto» si el proceso tiene un socket TCP con un puerto típico de minería."""
    inodos = set()
    try:
        for fd in os.listdir(f"{proc}/{pid}/fd"):
            try:
                m = re.match(r"socket:\[(\d+)\]", os.readlink(f"{proc}/{pid}/fd/{fd}"))
            except OSError:
                continue
            if m:
                inodos.add(m.group(1))
    except OSError:
        return None
    if not inodos:
        return None
    for tabla in ("tcp", "tcp6"):
        try:
            lineas = open(f"{proc}/{pid}/net/{tabla}").read().splitlines()[1:]
        except OSError:
            continue
        for ln in lineas:
            c = ln.split()
            if len(c) > 9 and c[9] in inodos:
                ip, puerto = c[2].rsplit(":", 1)
                if int(puerto, 16) in PUERTOS_MINERIA:
                    if len(ip) == 8:
                        ip = ".".join(str(b) for b in reversed(bytes.fromhex(ip)))
                    return f"{ip}:{int(puerto, 16)}"
    return None


def pantalla_bloqueada(run=subprocess.run) -> bool:
    """¿Todas las sesiones gráficas están bloqueadas? (si no hay ninguna, nadie está usando el equipo)."""
    try:
        r = run(["loginctl", "list-sessions", "--no-legend"], capture_output=True, text=True, timeout=5)
        ids = [ln.split()[0] for ln in r.stdout.splitlines() if ln.split()]
        estados = []
        for s in ids:
            p = run(["loginctl", "show-session", s, "-p", "LockedHint", "-p", "Type"], capture_output=True,
                    text=True, timeout=5).stdout
            if re.search(r"Type=(x11|wayland)", p):
                estados.append("LockedHint=yes" in p)
        return all(estados)
    except (OSError, subprocess.SubprocessError, IndexError):
        return False


class VigiaConsumo:
    def __init__(self, conexion=conexion_minera, bloqueada=pantalla_bloqueada):
        self.previa: dict[int, tuple[int, str, str]] = {}
        self.t_previo: float | None = None
        self.desde: dict[tuple[int, str], float] = {}        # (pid, exe) → inicio del uso alto
        self.bloqueo_desde: dict[tuple[int, str], float] = {}
        self.avisados: set[tuple[int, str]] = set()
        self.conexion, self.bloqueada = conexion, bloqueada

    def paso(self, actual: dict[int, tuple[int, str, str]], ahora: float) -> list[Alert]:
        alertas = []
        dt = (ahora - self.t_previo) if self.t_previo is not None else 0
        altos = {}
        if dt > 0:
            for pid, (ticks, exe, nombre) in actual.items():
                prev = self.previa.get(pid)
                if prev and prev[1] == exe and (ticks - prev[0]) / TICKS / dt >= UMBRAL_CPU:
                    altos[(pid, exe)] = nombre
        self.previa, self.t_previo = actual, ahora
        for k in list(self.desde):
            if k not in altos:
                self.desde.pop(k)
                self.bloqueo_desde.pop(k, None)
        bloqueada = None
        for k, nombre in altos.items():
            inicio = self.desde.setdefault(k, ahora - dt)
            if k in self.avisados:
                continue
            pid, exe = k
            minutos = max(1, round((ahora - inicio) / 60))
            if ahora - inicio >= SEG_GRAVE:
                destino = self.conexion(pid)
                if destino:
                    alertas.append(self._alerta(k, "high", f"«{os.path.basename(exe)}» (proceso {pid}) usa mucha "
                                                f"CPU desde hace {minutos} min y está conectado a un servicio de "
                                                f"minería de criptomonedas ({destino})"))
                    continue
                if DISFRAZ.match(nombre):
                    alertas.append(self._alerta(k, "high", f"El proceso {pid} se hace llamar «{nombre}» como si fuera "
                                                f"parte del sistema, pero es {exe} y usa mucha CPU desde hace "
                                                f"{minutos} min"))
                    continue
            if bloqueada is None:
                bloqueada = self.bloqueada()
            if not bloqueada:
                self.bloqueo_desde.pop(k, None)
                continue
            b0 = self.bloqueo_desde.setdefault(k, ahora - dt)
            if ahora - b0 >= SEG_BLOQUEADO:
                alertas.append(self._alerta(k, "medium", f"«{os.path.basename(exe)}» (proceso {pid}, en {exe}) usó "
                                            f"mucha CPU durante {round((ahora - b0) / 60)} min con la pantalla "
                                            "bloqueada. Si no lo dejaste trabajando tú, puede ser un minero."))
        self.avisados &= {k for k in self.avisados if k[0] in actual}
        return alertas

    def _alerta(self, k: tuple[int, str], sev: str, texto: str) -> Alert:
        self.avisados.add(k)
        return Alert("consumo_oculto", sev, texto, k[0], k[1], boton="ver_informe",
                     action="Si no reconoces ese programa, analízalo con Escudo o desinstálalo.")
