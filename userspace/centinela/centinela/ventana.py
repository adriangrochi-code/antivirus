"""Las apps de TISMA con interfaz web (TISMA Code, Rendimiento…) se abren como una aplicación más: ventana propia,
sin pestañas ni barra de direcciones, con su icono en la barra de tareas. Al cerrar la ventana se cierra la app.

Usa Chromium en modo aplicación con un perfil propio y temporal (en la memoria de la sesión, se borra al cerrar):
con un perfil compartido, una segunda ventana le pasaba su dirección a la primera y el proceso terminaba al
instante. Sin Chromium, el navegador predeterminado (como antes).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import webbrowser
from typing import Callable


def argumentos(exe: str, url: str, clase: str, perfil: str, tamano: str | None, entorno: dict) -> list[str]:
    # Por XWayland si está: en Wayland nativo Chromium ignora --class y la ventana salía con el icono de Chromium,
    # agrupada con el navegador (visto en QEMU); por X11 se asocia a su lanzador (StartupWMClass)
    x11 = ["--ozone-platform=x11"] if entorno.get("DISPLAY") and shutil.which("Xwayland") else []
    tam = [f"--window-size={tamano}"] if tamano else ["--start-maximized"]
    return [exe, *x11, f"--app={url}", f"--user-data-dir={perfil}", f"--class={clase}", "--no-first-run",
            "--no-default-browser-check", "--password-store=basic", "--disable-features=Translate", *tam]


def abrir_ventana(url: str, clase: str, tamano: str | None = None, al_cerrar: Callable[[], None] | None = None,
                  popen=subprocess.Popen, entorno: dict | None = None) -> bool:
    """Abre la app en su ventana. Devuelve True si es una ventana propia (y llamará a «al_cerrar» al cerrarla)."""
    entorno = os.environ if entorno is None else entorno
    exe = shutil.which("chromium") or shutil.which("chromium-browser")
    if not exe:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return False
    perfil = tempfile.mkdtemp(prefix=f"{clase}-", dir=entorno.get("XDG_RUNTIME_DIR") or None)
    proc = popen(argumentos(exe, url, clase, perfil, tamano, entorno),
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def esperar():
        proc.wait()
        shutil.rmtree(perfil, ignore_errors=True)
        if al_cerrar:
            al_cerrar()
    threading.Thread(target=esperar, daemon=True).start()
    return True
