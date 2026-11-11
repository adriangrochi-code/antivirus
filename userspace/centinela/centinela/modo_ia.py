"""Modo IA: mientras la IA local genera, el resto del sistema le deja el ancho de banda de memoria.

En un equipo con gráfica integrada (como el ThinkPad A275, un solo canal a ~13 GB/s) la CPU, la gráfica y el
compositor del escritorio comparten la misma memoria. Generar texto con un modelo local está limitado por ese
ancho de banda, así que todo lo demás que lo use en segundo plano le quita velocidad:

* en la sesión (lo hace TISMA Code, como el usuario): los efectos de KWin que releen la pantalla entera
  (desenfoque y contraste de fondo) y el indexador de archivos Baloo;
* en el sistema (lo hace centinela adaptd, como root): la recompresión de zram, la compactación de la memoria
  de apps en segundo plano y el escaneo de KSM (ksmd).

Señal: TISMA Code mantiene fresco /run/user/<uid>/tisma-modo-ia mientras genera (un «arriendo»). El lado de
root solo mira la FECHA del archivo, nunca su contenido; si TISMA Code se cierra o se cuelga, el arriendo caduca
y todo se reanuda solo. Nada de esto toca la seguridad: la protección en tiempo real de Escudo sigue igual.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import threading
import time
from typing import Callable

from . import sysfs

NOMBRE = "tisma-modo-ia"
FRESCO_S = 45                 # un arriendo más viejo que esto ya no cuenta (TISMA Code lo renueva cada 10 s)
RENUEVA_S = 10
RETENCION_S = 20              # entre dos pasos del agente no se restaura (evita encender y apagar sin parar)
# Efectos de KWin que releen o redibujan zonas grandes de la pantalla en cada fotograma
EFECTOS = ("blur", "contrast", "wobblywindows")


# ---------------------------------------------------------------- arriendo
def arriendo(uid: int | None = None) -> str:
    return f"/run/user/{os.getuid() if uid is None else uid}/{NOMBRE}"


def activo(now: Callable[[], float] = time.time) -> bool:
    """¿Algún usuario con la IA generando? (lado de root: solo la fecha de modificación, y del dueño correcto)."""
    for f in glob.glob(sysfs.path(f"/run/user/*/{NOMBRE}")):
        try:
            st = os.lstat(f)
            dueño = os.stat(os.path.dirname(f)).st_uid
        except OSError:
            continue
        if st.st_uid == dueño and now() - st.st_mtime < FRESCO_S:
            return True
    return False


# ---------------------------------------------------------------- sistema (root)
class Sistema:
    """Pausa y reanuda lo del sistema. Solo restaura lo que él mismo cambió."""
    KSM = "/sys/kernel/mm/ksm/run"

    def __init__(self, read=None, write=None):
        self.read = read or (lambda p: open(sysfs.path(p)).read().strip())
        self.write = write or self._write
        self.ksm_antes: str | None = None
        self.pausado = False

    @staticmethod
    def _write(p: str, v: str) -> None:
        with open(sysfs.path(p), "w") as fh:
            fh.write(v)

    def pausar(self) -> None:
        if self.pausado:
            return
        self.pausado = True
        try:
            v = self.read(self.KSM)
            if v == "1":                   # 1 = escaneando; 0 = parado (se conserva lo ya fusionado)
                self.write(self.KSM, "0")
                self.ksm_antes = v
        except OSError:
            pass

    def reanudar(self) -> None:
        if not self.pausado:
            return
        self.pausado = False
        if self.ksm_antes is not None:
            try:
                self.write(self.KSM, self.ksm_antes)
            except OSError:
                pass
            self.ksm_antes = None


# ---------------------------------------------------------------- sesión (usuario)
def _ok(r) -> bool:
    return r is not None and getattr(r, "returncode", 1) == 0


def sesion_entrar(run: Callable = subprocess.run) -> dict:
    """Quita los efectos pesados que estén cargados y suspende Baloo si estaba indexando. Devuelve lo cambiado."""
    cambios: dict = {"efectos": [], "baloo": False}
    kw = {"capture_output": True, "text": True, "timeout": 10}
    for e in EFECTOS:
        try:
            r = run(["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", "/Effects",
                     "--method", "org.kde.kwin.Effects.isEffectLoaded", e], **kw)
            if _ok(r) and "true" in r.stdout:
                r2 = run(["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", "/Effects",
                          "--method", "org.kde.kwin.Effects.unloadEffect", e], **kw)
                if _ok(r2):
                    cambios["efectos"].append(e)
        except (OSError, subprocess.SubprocessError):
            break                                       # sin gdbus o sin KWin: nada que hacer
    try:
        r = run(["balooctl6", "status"], **kw)
        out = (r.stdout or "").lower() if _ok(r) else ""
        if "running" in out and "suspend" not in out:
            if _ok(run(["balooctl6", "suspend"], **kw)):
                cambios["baloo"] = True
    except (OSError, subprocess.SubprocessError):
        pass
    return cambios


def sesion_salir(cambios: dict, run: Callable = subprocess.run) -> None:
    kw = {"capture_output": True, "text": True, "timeout": 10}
    for e in cambios.get("efectos", []):
        try:
            run(["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", "/Effects",
                 "--method", "org.kde.kwin.Effects.loadEffect", e], **kw)
        except (OSError, subprocess.SubprocessError):
            pass
    if cambios.get("baloo"):
        try:
            run(["balooctl6", "resume"], **kw)
        except (OSError, subprocess.SubprocessError):
            pass


class Generando:
    """Para TISMA Code: «with modo.generando():» alrededor de cada llamada al modelo local.

    Reentrante y con retención: entre dos pasos seguidos del agente no restaura los efectos (los apagaría y
    encendería cada pocos segundos). Guarda lo cambiado en un archivo para restaurarlo si TISMA Code se cerró
    a mitad (recuperar())."""

    def __init__(self, run: Callable = subprocess.run, uid: int | None = None, estado: str | None = None,
                 retencion: float = RETENCION_S, renueva: float = RENUEVA_S):
        self.run = run
        self.path = arriendo(uid)
        self.estado = estado or os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                                             "tisma-code", "modo-ia.json")
        self.retencion, self.renueva = retencion, renueva
        self._lock = threading.Lock()
        self._n = 0
        self._cambios: dict | None = None
        self._fin: threading.Timer | None = None
        self._latido: threading.Thread | None = None
        self._parar = threading.Event()

    def __enter__(self):
        with self._lock:
            self._n += 1
            if self._fin:
                self._fin.cancel()
                self._fin = None
            if self._cambios is None:
                self._tocar()
                self._parar.clear()
                self._latido = threading.Thread(target=self._latir, daemon=True)
                self._latido.start()
                self._cambios = sesion_entrar(self.run)
                self._guardar(self._cambios)
        return self

    def __exit__(self, *exc):
        with self._lock:
            self._n -= 1
            if self._n == 0 and self._cambios is not None:
                self._fin = threading.Timer(self.retencion, self.terminar)
                self._fin.daemon = True
                self._fin.start()
        return False

    def terminar(self) -> None:
        with self._lock:
            if self._n or self._cambios is None:
                return
            cambios, self._cambios = self._cambios, None
            self._parar.set()
        sesion_salir(cambios, self.run)
        for p in (self.path, self.estado):
            try:
                os.unlink(p)
            except OSError:
                pass

    def cerrar(self) -> None:
        """Al cerrar TISMA Code: restaura ya (la retención no llegaría a dispararse)."""
        with self._lock:
            self._n = 0
            if self._fin:
                self._fin.cancel()
                self._fin = None
        self.terminar()

    def _latir(self) -> None:
        while not self._parar.wait(self.renueva):
            self._tocar()

    def _tocar(self) -> None:
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
            os.utime(fd)
            os.close(fd)
        except OSError:
            pass                                     # sin /run/user (p. ej. fuera de una sesión): solo la sesión

    def _guardar(self, cambios: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.estado), exist_ok=True)
            with open(self.estado, "w") as fh:
                json.dump(cambios, fh)
        except OSError:
            pass

    def recuperar(self) -> bool:
        """Al abrir TISMA Code: si la vez anterior se cerró a mitad, deja el escritorio como estaba."""
        try:
            with open(self.estado) as fh:
                cambios = json.load(fh)
        except (OSError, ValueError):
            return False
        sesion_salir(cambios, self.run)
        try:
            os.unlink(self.estado)
        except OSError:
            pass
        return True
