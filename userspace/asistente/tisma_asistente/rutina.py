"""Rutinas: lista de acciones que se ejecutan al activar el asistente.

Configuración en ~/.config/tisma-asistente/rutina.json. Tipos de acción:
  {"tipo": "app",     "valor": "net.tisma.Code"}        aplicación por su ID de escritorio
  {"tipo": "perfil",  "valor": "rendimiento"}           perfil de potencia de centinela
  {"tipo": "comando", "valor": ["konsole", "--workdir", "~/proyectos"]}   sin shell
  {"tipo": "url",     "valor": "file:///home/yo/notas.md"}
  {"tipo": "decir",   "valor": "texto"}
Los comandos se ejecutan como lista de argumentos (nunca a través de un shell).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import voz

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tisma-asistente" / "rutina.json"
DEFAULT = {
    "saludo": "{saludo}. Sistemas en línea. {estado} Todo listo para trabajar.",
    "acciones": [
        {"tipo": "perfil", "valor": "rendimiento"},
        {"tipo": "app", "valor": "net.tisma.Code"},
        {"tipo": "app", "valor": "net.tisma.Tisma"},
        {"tipo": "app", "valor": "strata"},
    ],
}
PROFILES = {"silencioso", "equilibrado", "rendimiento"}


def load() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(DEFAULT, indent=2, ensure_ascii=False), encoding="utf-8")
        return dict(DEFAULT)
    except (OSError, ValueError) as e:
        # Un error de sintaxis del usuario NO borra su rutina: se avisa y se usa la predeterminada esta vez
        print(f"tisma-asistente: {CONFIG} tiene un error ({e}); se usa la rutina predeterminada sin modificarlo")
        return dict(DEFAULT)


def greeting(now: _dt.datetime | None = None) -> str:
    h = (now or _dt.datetime.now()).hour
    return "Buenos días" if 5 <= h < 13 else "Buenas tardes" if 13 <= h < 20 else "Buenas noches"


def system_status() -> str:
    """Resumen corto y local: IPs bloqueadas por Escudo y amenazas en cuarentena."""
    parts = []
    try:
        blocked = json.loads(Path("/var/lib/escudo/blocked_ips.json").read_text())
        if blocked:
            parts.append(f"Escudo ha bloqueado {len(blocked)} direcciones atacantes.")
    except (OSError, ValueError):
        pass
    q = Path("/var/lib/escudo/quarantine")
    try:
        n = len(list(q.glob("*.json")))
        if n:
            parts.append(f"Hay {n} archivos en cuarentena.")
    except OSError:
        pass
    return " ".join(parts) or "Sin amenazas detectadas."


def run_action(a: dict, dry_run: bool = False) -> str:
    kind, val = a.get("tipo"), a.get("valor")
    if kind == "app":
        if not isinstance(val, str) or "/" in val:
            raise ValueError("app: usa el ID de escritorio, p. ej. net.tisma.Code")
        argv = ["gtk-launch", val] if shutil.which("gtk-launch") else ["kioclient", "exec", f"{val}.desktop"]
    elif kind == "perfil":
        if val not in PROFILES:
            raise ValueError(f"perfil inválido: {val}")
        # Unidad de systemd con permiso de polkit para la sesión local (sin contraseña)
        argv = ["systemctl", "start", f"centinela-perfil@{val}.service"]
    elif kind == "comando":
        if not (isinstance(val, list) and val and all(isinstance(x, str) for x in val)):
            raise ValueError("comando: lista de argumentos, p. ej. [\"konsole\"]")
        argv = [os.path.expanduser(x) for x in val]
    elif kind == "url":
        argv = ["xdg-open", str(val)]
    elif kind == "decir":
        if not dry_run:
            voz.say(str(val))
        return f"decir: {val}"
    else:
        raise ValueError(f"tipo de acción desconocido: {kind}")
    if not dry_run:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return " ".join(argv)


def run(dry_run: bool = False) -> list[str]:
    cfg = load()
    try:
        text = cfg.get("saludo", "").format(saludo=greeting(), estado=system_status())
    except (KeyError, IndexError, ValueError) as e:      # llaves desconocidas en el saludo del usuario
        text = f"{greeting()}. (Revisa el saludo de tu rutina: {e})"
    done = []
    if text:
        done.append(f"decir: {text}")
        if not dry_run:
            import threading
            threading.Thread(target=voz.say, args=(text,), daemon=True).start()
    for a in cfg.get("acciones", []):
        try:
            done.append(run_action(a, dry_run))
        except (ValueError, OSError) as e:
            done.append(f"error: {e}")
    return done
