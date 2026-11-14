"""Zona segura desechable: abrir un archivo dudoso (adjunto, descarga) sin que pueda dañar nada.

Idea tomada de Qubes OS (las «máquinas desechables»), en liviano para equipos modestos:
* El archivo se COPIA a una carpeta temporal en la memoria (XDG_RUNTIME_DIR, tmpfs): el original no se toca.
* Se abre con el visor que corresponda dentro de bubblewrap: sin internet, sin ninguno de tus archivos (su
  $HOME es esa carpeta temporal), sin D-Bus (no puede hablar con el resto del escritorio), sin selector de
  archivos, sin GPU (menos superficie de ataque) y sin capacidades.
* Al cerrar el visor se borra todo: lo que el archivo haya hecho dentro desaparece con él.

Para analizar programas sospechosos está Escudo (máquina virtual); esto es para MIRAR documentos.
"""
from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import profile as prof

TAMANO_MAX = 2 * 1024 ** 3                      # 2 GB: la copia va a la memoria

# (visor, argumentos antes del archivo, audio, extensiones). El primero instalado que sirva.
VISORES = [
    ("/usr/bin/okular", [], False, {".pdf", ".epub", ".djvu", ".cbz", ".cbr", ".xps", ".md", ".txt", ".ps"}),
    ("/usr/bin/gwenview", [], False, {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
                                     ".svg", ".heic", ".avif"}),
    ("/usr/bin/libreoffice", ["--norestore", "--nologo", "--nolockcheck", "--view"], False,
     {".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp"}),
    ("/usr/bin/haruna", [], True, {".mp4", ".mkv", ".webm", ".avi", ".mov", ".mp3", ".ogg", ".opus", ".flac",
                                   ".wav", ".m4a"}),
    ("/usr/bin/kate", [], False, {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log", ".ini"}),
]


class NoSePuede(Exception):
    """Motivo en lenguaje simple."""


def visor_para(ruta: Path, existe=os.path.exists) -> tuple[str, list[str], bool]:
    ext = ruta.suffix.lower()
    for exe, args, audio, exts in VISORES:
        if ext in exts and existe(exe):
            return exe, args, audio
    raise NoSePuede(f"No hay un visor para archivos «{ext or 'sin extensión'}» en la zona segura.")


def perfil(nombre: str, exe: str, audio: bool) -> prof.Profile:
    """Lo mínimo para mostrar el archivo: ventana (Wayland) y, para vídeo o música, sonido. Nada más."""
    p = prof.Profile(nombre=nombre, exec=exe, descripcion="Zona segura desechable", red=False, gpu=False,
                     audio=audio, camara=False, mandos=False, wayland=True, x11=False, portales=False,
                     rw=[], ro=[], dbus_talk=[], dbus_own=[],
                     env={"QT_QPA_PLATFORM": "wayland", "SAL_USE_VCLPLUGIN": "qt6"})
    p.validate()
    return p


def copiar(origen: Path, destino_dir: Path) -> Path:
    """Copia solo archivos normales, sin seguir enlaces fuera de lo que el usuario eligió."""
    real = origen.resolve()
    st = os.stat(real)
    if not stat.S_ISREG(st.st_mode):
        raise NoSePuede(f"«{origen.name}» no es un archivo.")
    if st.st_size > TAMANO_MAX:
        raise NoSePuede(f"«{origen.name}» es demasiado grande para la zona segura (más de 2 GB).")
    nombre = "".join(c if c.isalnum() or c in "._- " else "_" for c in origen.name)[:120] or "archivo"
    destino_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destino = destino_dir / nombre
    shutil.copyfile(real, destino)
    os.chmod(destino, 0o600)
    return destino


def carpeta_temporal(base: str | None = None) -> Path:
    base = base or os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    d = Path(base) / f"tisma-zona-{secrets.token_hex(6)}"
    d.mkdir(mode=0o700)
    return d


def avisar(texto: str, icono: str = "security-high") -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Zona segura", "-i", icono, "Zona segura", texto], check=False)


def abrir(ruta: Path, ejecutar=None, base: str | None = None) -> int:
    from .cli import run
    ejecutar = ejecutar or run
    try:
        exe, args, audio = visor_para(ruta)
        tmp = carpeta_temporal(base)
    except (NoSePuede, OSError) as e:
        avisar(str(e), "dialog-warning")
        return 2
    try:
        copia = copiar(ruta, tmp / "home" / "archivo")
        nombre = f"zona-{tmp.name[-12:]}"
        p = perfil(nombre, exe, audio)
        avisar(f"«{ruta.name}» se abrió sin internet y sin acceso a tus archivos. Al cerrarlo se borra todo lo "
               "que haya pasado dentro; tu archivo original no cambia.")
        dentro = f"{Path.home()}/archivo/{copia.name}"
        return ejecutar(p, [*args, dentro], app_home=tmp / "home")
    except NoSePuede as e:
        avisar(str(e), "dialog-warning")
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)     # bubblewrap ya terminó: no queda nada montado aquí


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("uso: tisma-zona-segura <archivo>", file=sys.stderr)
        return 2
    return abrir(Path(argv[0]))
