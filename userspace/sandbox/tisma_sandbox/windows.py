"""Doble clic en un programa de Windows (.exe, .msi): como en Windows, pero revisado y aislado.

1. Si falta Wine, se ofrece instalarlo (perfil «windows»).
2. El programa se copia a la carpeta privada de su aislamiento y Escudo revisa ESA copia (firmas, YARA, ML y lo que
   hace al ejecutarlo con Wine en un entorno de prueba): se ejecuta exactamente lo revisado.
   - limpio → se abre;
   - sospechoso → se avisa con los motivos; un administrador puede permitirlo (escudo permitir);
   - malicioso → no se abre.
   Sin Escudo funcionando no se abre nada (falla cerrado).
3. Se abre con Wine dentro de tisma-sandbox: SIN red, con su propio «Windows» (prefijo de Wine) por programa, sin
   ver la carpeta personal: solo ~/Windows/<programa> para intercambiar archivos (unidad Z: de Wine).
4. Si era un instalador, los programas que instaló aparecen en el menú de inicio; al abrirlos, Escudo revisa su .exe
   igual que la primera vez (lo ya revisado se abre al instante).
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import profile as prof

EXTENSIONES = (".exe", ".msi", ".msp")
MENU = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications/tisma-windows"
INTERCAMBIO = Path.home() / "Windows"
TITULO = "Programas de Windows"


# ---------------------------------------------------------------- avisos al usuario
def _kdialog(*args: str) -> int:
    if not shutil.which("kdialog"):
        print(" ".join(args[1:2]), file=sys.stderr)
        return 1
    return subprocess.call(["kdialog", "--title", TITULO, *args])


def avisar(texto: str, icono: str = "wine") -> None:
    if shutil.which("notify-send"):
        subprocess.call(["notify-send", "-a", "Escudo", "-i", icono, TITULO, texto])
    else:
        print(texto, file=sys.stderr)


def error(texto: str) -> None:
    _kdialog("--error", texto)


def preguntar(texto: str, si: str, no: str = "Cancelar") -> bool:
    return _kdialog("--yesno", texto, "--yes-label", si, "--no-label", no) == 0


# ---------------------------------------------------------------- piezas
def nombre_perfil(nombre: str, sha: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", Path(nombre).stem.lower()).strip("-")[:40] or "programa"
    return f"windows-{base}-{sha[:8]}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def perfil(nombre: str, carpeta: str) -> prof.Profile:
    """El aislamiento de UN programa de Windows: su $HOME privado (con su prefijo de Wine) y su carpeta de
    intercambio; sin red, con gráficos, sonido y mandos."""
    p = prof.Profile(nombre=nombre, exec="/usr/bin/wine", descripcion="Programa de Windows aislado", red=False,
                     gpu=True, audio=True, mandos=True, x11=True, portales=False, rw=[f"~/Windows/{carpeta}"],
                     env={"WINEPREFIX": f"{Path.home()}/.wine", "WINEDEBUG": "-all"})
    p.validate()
    return p


def revisar(ruta: Path, pedir=None) -> dict:
    """Pregunta a Escudo por el archivo (abierto, no por su ruta)."""
    if pedir is None:
        sys.path.insert(0, "/usr/lib/centinela/python")
        from escudo.consulta import pedir_revision as pedir
    try:
        return pedir(str(ruta), al_avanzar=lambda d: avisar(
            f"Escudo está revisando «{ruta.name}» antes de abrirlo (la primera vez puede tardar unos minutos).",
            "security-medium"))
    except (OSError, ConnectionError) as e:
        return {"estado": "error", "mensaje": f"Escudo no responde ({e})", "sin_escudo": True}


def decidir(r: dict, nombre: str, ruta_original: Path, permitir=None) -> bool:
    """¿Se abre? Muestra el motivo si no."""
    if r.get("estado") != "listo":
        error(f"No se abrió «{nombre}»: {r.get('mensaje') or 'Escudo no pudo revisarlo'}.\n\n"
              "Por seguridad, los programas de Windows solo se abren después de que Escudo los revise.")
        return False
    v = r.get("veredicto")
    motivos = "\n".join(f"• {m}" for m in (r.get("motivos") or [])[:5])
    if v == "limpio":
        return True
    if v == "malicioso":
        error(f"«{nombre}» es MALICIOSO y no se abrió.\n\n{motivos}\n\nTe recomendamos borrarlo.")
        return False
    texto = (f"Escudo vio un comportamiento sospechoso en «{nombre}»:\n\n{motivos or '• sin detalles'}\n\n"
             "No lo abras si no confías en quién te lo dio. Si confías, un administrador puede permitirlo.")
    if not preguntar(texto, "Permitir (administrador)", "No abrir"):
        return False
    permitir = permitir or (lambda p: subprocess.call(["pkexec", "/usr/bin/escudo", "permitir", str(p)]) == 0)
    if permitir(ruta_original):
        return True
    error("No se pudo permitir el programa (hacen falta permisos de administrador).")
    return False


def wine_listo() -> bool:
    if shutil.which("wine"):
        return True
    if not preguntar("Para abrir programas de Windows hace falta Wine, que no está instalado.\n\n¿Descargarlo e "
                     "instalarlo ahora? (unos cientos de MB; para programas de 32 bits hay que reiniciar después)",
                     "Instalar Wine"):
        return False
    avisar("Instalando Wine… te avisamos al terminar.")
    if subprocess.call(["pkexec", "/usr/bin/tisma-perfil", "instalar", "windows"]) != 0 or not shutil.which("wine"):
        error("No se pudo instalar Wine. Revisa la conexión a Internet e inténtalo de nuevo.")
        return False
    return True


def menu_instalados(nombre: str, home_app: Path, menu: Path = MENU) -> list[Path]:
    """Tras un instalador: los accesos que Wine creó DENTRO del aislamiento pasan al menú real. Del archivo de Wine
    solo se toman el nombre y la ruta de Windows del programa; la orden Exec la escribe TISMA (nunca se copia)."""
    origen = home_app / ".local/share/applications/wine"
    creados = []
    if not origen.is_dir():
        return creados
    menu.mkdir(parents=True, exist_ok=True)
    for d in sorted(origen.rglob("*.desktop"))[:30]:
        try:
            texto = d.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        nom = re.search(r"^Name=(.{1,80})$", texto, re.M)
        exe = re.search(r"^Exec=.*?\bwine\b\s+(.+)$", texto, re.M)
        if not (nom and exe):
            continue
        try:
            # Primero el escape de los valores .desktop (\\\\ → \\) y luego el de los argumentos de Exec
            partes = shlex.split(exe.group(1).replace("\\\\", "\\"))
        except ValueError:
            continue
        win = next((p for p in partes if re.match(r"^[A-Za-z]:\\", p) and p.lower().endswith((".exe", ".lnk"))),
                   None)
        if not win or "\n" in win or len(win) > 400:
            continue
        nombre_vis = re.sub(r"[\x00-\x1f]", "", nom.group(1))
        destino = menu / f"{nombre}-{hashlib.sha256(win.encode()).hexdigest()[:8]}.desktop"
        # La ruta de Windows va en un campo propio (escapado como valor de .desktop), no en Exec: el acceso se pasa
        # a sí mismo (%k) y el lanzador lee de él a qué aislamiento y programa corresponde
        destino.write_text("[Desktop Entry]\nType=Application\n"
                           f"Name={nombre_vis}\nComment=Programa de Windows (aislado, sin Internet)\n"
                           "Exec=tisma-abrir-windows --acceso %k\nIcon=wine\nCategories=Wine;\n"
                           f"X-TISMA-Perfil={nombre}\nX-TISMA-Exe={win.replace(chr(92), chr(92) * 2)}\n",
                           encoding="utf-8")
        creados.append(destino)
    return creados


def ruta_windows(home_app: Path, win: str) -> Path | None:
    """C:\\Program Files\\X\\x.exe → el archivo dentro de ESE prefijo (nunca fuera de él)."""
    m = re.match(r"^([A-Za-z]):\\(.+)$", win)
    if not m or m.group(1).lower() != "c":
        return None
    raiz = (home_app / ".wine/drive_c").resolve()
    p = (raiz / m.group(2).replace("\\", "/")).resolve()
    return p if raiz in p.parents and p.is_file() else None


def destino_lnk(lnk: Path) -> str | None:
    """Ruta de Windows a la que apunta un acceso directo .lnk (formato MS-SHLLINK: LinkInfo → LocalBasePath)."""
    try:
        b = lnk.read_bytes()[:65536]
    except OSError:
        return None
    if len(b) < 0x4C or int.from_bytes(b[0:4], "little") != 0x4C:
        return None
    flags = int.from_bytes(b[0x14:0x18], "little")
    i = 0x4C
    if flags & 0x1:                                    # HasLinkTargetIDList
        i += 2 + int.from_bytes(b[i:i + 2], "little")
    if not flags & 0x2 or i + 28 > len(b):             # sin LinkInfo
        return None
    li = i
    cab = int.from_bytes(b[li + 4:li + 8], "little")
    li_flags = int.from_bytes(b[li + 8:li + 12], "little")
    if not li_flags & 0x1:                             # sin VolumeIDAndLocalBasePath
        return None

    def cadena(off: int, unicode: bool) -> str:
        if not off or li + off >= len(b):
            return ""
        if unicode:
            fin = b.find(b"\0\0", li + off)
            while fin >= 0 and (fin - li - off) % 2:
                fin = b.find(b"\0\0", fin + 1)
            return b[li + off:fin if fin >= 0 else None].decode("utf-16-le", "replace")
        fin = b.find(b"\0", li + off)
        return b[li + off:fin if fin >= 0 else None].decode("cp1252", "replace")
    if cab >= 0x24:
        base = cadena(int.from_bytes(b[li + 28:li + 32], "little"), True)
        suf = cadena(int.from_bytes(b[li + 32:li + 36], "little"), True)
    else:
        base, suf = "", ""
    if not base:
        base = cadena(int.from_bytes(b[li + 16:li + 20], "little"), False)
        suf = cadena(int.from_bytes(b[li + 24:li + 28], "little"), False)
    destino = base + suf
    return destino if re.match(r"^[A-Za-z]:\\", destino) and len(destino) < 400 else None


def ejecutar(p: prof.Profile, args: list[str]) -> int:
    from .cli import run
    return run(p, args)


# ---------------------------------------------------------------- flujo
def abrir(ruta: Path) -> int:
    ruta = ruta.resolve()
    if not ruta.is_file() or ruta.suffix.lower() not in EXTENSIONES:
        error(f"«{ruta.name}» no es un programa de Windows (.exe o .msi).")
        return 2
    if not wine_listo():
        return 1
    sha = sha256(ruta)
    nombre = nombre_perfil(ruta.name, sha)
    home_app = prof.DATA_DIR / nombre
    copia_dir = home_app / "programa"
    copia_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    copia = copia_dir / re.sub(r"[^\w.+-]", "_", ruta.name)[:120]
    shutil.copyfile(ruta, copia)
    if sha256(copia) != sha:
        error(f"«{ruta.name}» cambió mientras se copiaba. Vuelve a intentarlo.")
        return 1
    if not decidir(revisar(copia), ruta.name, ruta):
        return 1
    carpeta = nombre.removeprefix("windows-")
    p = perfil(nombre, carpeta)
    (INTERCAMBIO / carpeta).mkdir(parents=True, exist_ok=True)
    avisar(f"Abriendo «{ruta.name}» sin acceso a Internet. Para pasarle archivos usa la carpeta "
           f"Windows/{carpeta} de tu carpeta personal (unidad Z: dentro del programa).")
    dentro = f"{Path.home()}/programa/{copia.name}"
    rc = ejecutar(p, ["msiexec", "/i", dentro] if copia.suffix.lower() in (".msi", ".msp") else [dentro])
    nuevos = menu_instalados(nombre, home_app)
    if nuevos:
        avisar(f"Se instalaron {len(nuevos)} programa(s) de Windows: están en el menú de inicio.")
    return rc


def abrir_instalado(nombre: str, win: str) -> int:
    """Un programa instalado antes en su aislamiento: Escudo revisa su .exe real antes de cada apertura."""
    if not prof._NAME.match(nombre) or not nombre.startswith("windows-"):
        error("Acceso directo no válido.")
        return 2
    home_app = prof.DATA_DIR / nombre
    exe = ruta_windows(home_app, win)
    if exe and exe.suffix.lower() == ".lnk":
        # El acceso directo de Windows se resuelve aquí: Escudo revisa el .exe real y se ejecuta ese, no el .lnk
        win = destino_lnk(exe) or ""
        exe = ruta_windows(home_app, win) if win.lower().endswith(".exe") else None
    if not exe:
        error("El programa ya no está instalado.")
        return 1
    if not wine_listo() or not decidir(revisar(exe), exe.name, exe):
        return 1
    carpeta_win = win.rsplit("\\", 1)[0]
    return ejecutar(perfil(nombre, nombre.removeprefix("windows-")), ["start", "/wait", "/d", carpeta_win, win])


def leer_acceso(acceso: Path, menu: Path = MENU) -> tuple[str, str] | None:
    """(perfil, ruta de Windows) de un acceso creado por menu_instalados (solo los de NUESTRA carpeta del menú)."""
    try:
        acceso = acceso.resolve()
        if menu.resolve() not in acceso.parents:
            return None
        texto = acceso.read_text(encoding="utf-8")
    except OSError:
        return None
    nom = re.search(r"^X-TISMA-Perfil=(windows-[a-z0-9._-]{1,60})$", texto, re.M)
    exe = re.search(r"^X-TISMA-Exe=(.{4,800})$", texto, re.M)
    if not (nom and exe):
        return None
    return nom.group(1), exe.group(1).replace("\\\\", "\\")


def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) == 2 and a[0] == "--acceso":
        datos = leer_acceso(Path(a[1]))
        if not datos:
            error("Acceso directo no válido.")
            return 2
        return abrir_instalado(*datos)
    if len(a) != 1:
        print("uso: tisma-abrir-windows <programa.exe|instalador.msi>", file=sys.stderr)
        return 2
    return abrir(Path(a[0]))
