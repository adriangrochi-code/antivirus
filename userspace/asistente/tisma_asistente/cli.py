"""tisma-asistente: doble palmada → saludo con voz local → rutina de trabajo.

  tisma-asistente escuchar        escucha el micrófono (lo usa el inicio automático)
  tisma-asistente rutina          ejecuta la rutina ahora (también sirve como atajo de teclado)
  tisma-asistente decir "texto"   prueba la voz
  tisma-asistente activar|desactivar   inicio automático con la sesión (desactivado por defecto)
  tisma-asistente estado
"""

from __future__ import annotations

import argparse
import shutil
import threading
from pathlib import Path

from . import clap, rutina, voz

AUTOSTART = Path.home() / ".config/autostart/tisma-asistente.desktop"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tisma-asistente", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("escuchar")
    e.add_argument("--sensibilidad", type=float, default=7.0, help="más alto = menos falsos positivos")
    r = sub.add_parser("rutina")
    r.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("decir")
    d.add_argument("texto")
    for n in ("activar", "desactivar", "estado"):
        sub.add_parser(n)
    a = ap.parse_args(argv)

    if a.cmd == "rutina":
        for line in rutina.run(dry_run=a.dry_run):
            print(line)
    elif a.cmd == "decir":
        return 0 if voz.say(a.texto) else 1
    elif a.cmd == "escuchar":
        dev = clap.pick_input_device()
        print(f"Escuchando (micrófono {dev}). Doble palmada para empezar. Ctrl+C para salir.")
        busy = threading.Lock()

        def fire():
            if busy.acquire(blocking=False):
                try:
                    print("Doble palmada detectada")
                    rutina.run()
                finally:
                    busy.release()
        try:
            clap.listen(fire, device=dev, cfg=clap.ClapConfig(spike_ratio=a.sensibilidad))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "activar":
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        src = Path("/usr/share/tisma-asistente/tisma-asistente.desktop")
        AUTOSTART.write_text(src.read_text() if src.exists() else
                             "[Desktop Entry]\nType=Application\nName=TISMA Asistente\n"
                             "Exec=tisma-asistente escuchar\nIcon=tisma\nX-KDE-autostart-phase=2\n")
        print("Activado: escuchará el micrófono al iniciar sesión (todo se procesa en tu equipo).")
    elif a.cmd == "desactivar":
        AUTOSTART.unlink(missing_ok=True)
        print("Desactivado.")
    elif a.cmd == "estado":
        print(f"Inicio automático: {'sí' if AUTOSTART.exists() else 'no'}")
        print(f"Voz: {voz.engine()}" + (f" ({voz.piper_voice().name})" if voz.engine() == 'piper' else ""))
        print(f"Rutina: {rutina.CONFIG}")
        print(f"Audio: {'pw-play' if shutil.which('pw-play') else 'aplay/paplay'}")
    return 0
