"""Copias de seguridad de TISMA OS.

Dos capas, porque protegen de cosas distintas:

* Instantáneas btrfs de SOLO LECTURA de / y /home en /.snapshots (subvolumen @snapshots).
  Instantáneas diarias y antes de cada actualización (gancho de apt). Son instantáneas (copy-on-write: casi
  no ocupan) y deshacen en segundos una actualización rota, un borrado o archivos cifrados por un ransomware:
  un proceso del usuario no puede modificarlas. NO protegen si falla el disco.
* Copia externa con restic (cifrada y deduplicada) en un disco externo o carpeta elegida: protege del fallo
  o robo del equipo. Si el destino no está conectado, la copia se omite y se reintenta al día siguiente.

Volver atrás el sistema: se crea una instantánea de seguridad, el subvolumen @ actual se renombra a
@antes-<fecha> y una copia escribible de la instantánea elegida pasa a ser @. Se aplica al reiniciar.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SNAPDIR = Path(os.environ.get("TISMA_COPIAS_SNAPDIR", "/.snapshots"))
CONFIG = Path(os.environ.get("TISMA_COPIAS_CONFIG", "/etc/tisma/copias.json"))
KEYFILE = Path(os.environ.get("TISMA_COPIAS_CLAVE", "/etc/tisma/copias.clave"))
TOPMNT = Path("/run/tisma-copias/raiz")
MOTIVOS = ("diaria", "actualizacion", "manual", "antes-restaurar")
# Cuántas instantáneas se conservan de cada tipo.
RETENCION = {"diaria": 7, "actualizacion": 6, "manual": 10, "antes-restaurar": 3}
NAME_RE = re.compile(r"^(\d{8}-\d{6})-(" + "|".join(MOTIVOS) + r")$")
EXCLUIR = ["/home/*/.cache", "/home/*/.local/share/Trash", "/home/*/.local/share/tisma-code/models",
           "/home/*/.local/share/waydroid", "/home/*/.steam/steam/steamapps", "/home/*/.var/app/*/cache",
           "/home/*/Descargas/*.iso"]


class CopiasError(RuntimeError):
    pass


def run(argv: list[str], env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(argv, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise CopiasError(f"{' '.join(argv[:3])}…: {(r.stderr or r.stdout).strip()[-500:]}")
    return r


@dataclass
class Montaje:
    fstype: str
    source: str        # /dev/mapper/luks-… o /dev/nvme0n1p2
    subvol: str        # /@ (btrfs) o ""


def montaje(path: str = "/", runner=run) -> Montaje:
    out = runner(["findmnt", "-n", "-o", "FSTYPE,SOURCE,OPTIONS", "--target", path]).stdout.split()
    if len(out) < 3:
        raise CopiasError(f"no se pudo leer el montaje de {path}")
    fstype, source, opts = out[0], out[1], out[2]
    m = re.search(r"(?:^|,)subvol=([^,]+)", opts)
    return Montaje(fstype, re.sub(r"\[.*\]$", "", source), m.group(1) if m else "")


def _now() -> _dt.datetime:
    return _dt.datetime.now()


# ---------------------------------------------------------------- instantáneas
def instantaneas(snapdir: Path = SNAPDIR) -> list[dict]:
    out = []
    try:
        entries = sorted(os.listdir(snapdir))
    except OSError:
        return []
    for name in entries:
        m = NAME_RE.match(name)
        if not m or not (snapdir / name).is_dir():
            continue
        meta = {}
        try:
            meta = json.loads((snapdir / f"{name}.json").read_text())
        except (OSError, ValueError):
            pass
        out.append({"nombre": name, "fecha": m.group(1), "motivo": m.group(2),
                    "home": (snapdir / f"{name}-home").is_dir(), "nota": meta.get("nota", "")})
    return out


def crear(motivo: str = "manual", nota: str = "", runner=run, snapdir: Path = SNAPDIR,
          min_intervalo_s: int = 600) -> str | None:
    if motivo not in MOTIVOS:
        raise CopiasError(f"motivo desconocido: {motivo}")
    if montaje("/", runner).fstype != "btrfs":
        raise CopiasError("la raíz no es btrfs: las instantáneas no están disponibles (usa la copia externa)")
    if not snapdir.is_dir():
        raise CopiasError(f"{snapdir} no existe (subvolumen @snapshots)")
    now = _now()
    if motivo == "actualizacion":           # apt puede invocar el gancho varias veces seguidas
        for s in reversed(instantaneas(snapdir)):
            t = _dt.datetime.strptime(s["fecha"], "%Y%m%d-%H%M%S")
            if s["motivo"] == "actualizacion" and (now - t).total_seconds() < min_intervalo_s:
                return None
    name = f"{now:%Y%m%d-%H%M%S}-{motivo}"
    runner(["btrfs", "subvolume", "snapshot", "-r", "/", str(snapdir / name)])
    try:
        if montaje("/home", runner).subvol not in ("", montaje("/", runner).subvol):
            runner(["btrfs", "subvolume", "snapshot", "-r", "/home", str(snapdir / f"{name}-home")])
    except CopiasError:
        pass                                   # /home en otro sistema de archivos: solo la raíz
    (snapdir / f"{name}.json").write_text(json.dumps({"motivo": motivo, "nota": nota[:200],
                                                      "creada": now.isoformat(timespec="seconds")}))
    return name


def borrar(nombre: str, runner=run, snapdir: Path = SNAPDIR) -> None:
    if not NAME_RE.match(nombre):
        raise CopiasError(f"nombre de instantánea no válido: {nombre}")
    for sub in (f"{nombre}-home", nombre):
        if (snapdir / sub).is_dir():
            runner(["btrfs", "subvolume", "delete", str(snapdir / sub)])
    (snapdir / f"{nombre}.json").unlink(missing_ok=True)


def podar(runner=run, snapdir: Path = SNAPDIR) -> list[str]:
    """Aplica la retención por tipo; nunca borra la más reciente de cada tipo."""
    borradas = []
    for motivo, keep in RETENCION.items():
        snaps = [s["nombre"] for s in instantaneas(snapdir) if s["motivo"] == motivo]
        for name in snaps[: max(0, len(snaps) - keep)]:
            borrar(name, runner, snapdir)
            borradas.append(name)
    return borradas


def _snap_path(nombre: str, ruta: str, snapdir: Path) -> tuple[Path, Path]:
    if not NAME_RE.match(nombre):
        raise CopiasError(f"nombre de instantánea no válido: {nombre}")
    dest = Path(os.path.abspath(ruta))
    if dest.is_relative_to("/home"):
        base = snapdir / f"{nombre}-home"
        rel = dest.relative_to("/home")
        if not base.is_dir():
            base, rel = snapdir / nombre, dest.relative_to("/")
    else:
        base, rel = snapdir / nombre, dest.relative_to("/")
    src = base / rel
    if not os.path.realpath(src).startswith(str(base) + os.sep) and src != base:
        raise CopiasError("la ruta sale de la instantánea")
    return src, dest


def restaurar_archivo(nombre: str, ruta: str, runner=run, snapdir: Path = SNAPDIR) -> str:
    """Devuelve un archivo o carpeta a como estaba en la instantánea. Lo actual se conserva al lado como
    «<nombre>.antes-<fecha>» (nunca se pierde nada).

    Seguridad: si la carpeta de destino es de un usuario, la escritura se hace CON LA IDENTIDAD de ese usuario
    (root solo lee la instantánea, que es de solo lectura, y le pasa los datos con tar). Así un usuario que cambie
    el destino por un enlace a /etc/passwd en mitad de la restauración solo puede escribir donde ya podía."""
    if not os.path.isabs(ruta):
        raise CopiasError("indica la ruta completa (con pkexec la carpeta actual es /root)")
    src, dest = _snap_path(nombre, ruta, snapdir)
    if os.path.realpath(dest.parent) != str(dest.parent):
        raise CopiasError("la carpeta de destino pasa por un enlace simbólico: indica la ruta real")
    if not src.exists() and not src.is_symlink():
        raise CopiasError(f"{ruta} no existe en la instantánea {nombre}")
    st = os.stat(dest.parent)
    keep = dest.with_name(f"{dest.name}.antes-{_now():%Y%m%d-%H%M%S}")
    exists = dest.exists() or dest.is_symlink()
    if st.st_uid == 0 and not st.st_mode & 0o022:
        # Carpeta de root que nadie más puede modificar: no hay carrera posible
        if exists:
            os.rename(dest, keep)
        runner(["cp", "-a", "--reflink=auto", "--", str(src), str(dest)])
        return str(dest)
    if st.st_uid == 0:
        raise CopiasError("la carpeta de destino de root es escribible por otros: restaura en otra ubicación")
    as_user = ["setpriv", f"--reuid={st.st_uid}", f"--regid={st.st_gid}", "--clear-groups", "--"]
    if exists:
        runner(as_user + ["mv", "-T", "--", str(dest), str(keep)])
    runner(["bash", "-o", "pipefail", "-c", 'tar -C "$1" -cf - -- "$2" | "${@:4}" tar -C "$3" -xpf -',
            "restaurar", str(src.parent), src.name, str(dest.parent), *as_user])
    return str(dest)


def volver_atras(nombre: str, runner=run, snapdir: Path = SNAPDIR, topmnt: Path = TOPMNT) -> str:
    """Hace que el sistema arranque desde la instantánea `nombre` (solo la raíz; /home no se toca)."""
    if not NAME_RE.match(nombre) or not (snapdir / nombre).is_dir():
        raise CopiasError(f"no existe la instantánea {nombre}")
    root = montaje("/", runner)
    if root.fstype != "btrfs" or root.subvol.strip("/") != "@":
        raise CopiasError("la vuelta atrás necesita la raíz en el subvolumen @ (instalación btrfs de TISMA)")
    crear("antes-restaurar", f"antes de volver a {nombre}", runner, snapdir)
    topmnt.mkdir(parents=True, exist_ok=True, mode=0o700)
    runner(["mount", "-o", "subvolid=5", root.source, str(topmnt)])
    try:
        snaps_sub = montaje(str(snapdir), runner).subvol.strip("/") or "@snapshots"
        stamp = f"{_now():%Y%m%d-%H%M%S}"
        runner(["btrfs", "subvolume", "snapshot", str(topmnt / snaps_sub / nombre), str(topmnt / "@nuevo")])
        os.rename(topmnt / "@", topmnt / f"@antes-{stamp}")
        os.rename(topmnt / "@nuevo", topmnt / "@")
    finally:
        runner(["umount", str(topmnt)], check=False)
    return f"@antes-{stamp}"


# ---------------------------------------------------------------- autorreparación (DUP)
def perfil_datos(runner=run) -> tuple[str, int, int]:
    """(perfil de datos, bytes de datos usados, bytes libres estimados) de la raíz btrfs."""
    out = runner(["btrfs", "filesystem", "usage", "-b", "/"]).stdout
    prof = re.search(r"^Data,(\w+):.*?Used:(\d+)", out, re.M | re.S)
    free = re.search(r"Free \(estimated\):\s+(\d+)", out)
    if not prof or not free:
        raise CopiasError("no se pudo leer el uso de btrfs")
    return prof.group(1).lower(), int(prof.group(2)), int(free.group(1))


def autorreparacion(activar: bool, runner=run) -> str:
    """Datos en DUP: dos copias de cada bloque en el mismo disco. Si un bloque se corrompe (sector dañado, error
    de escritura), btrfs lo detecta por su checksum y lo repara solo con la otra copia. Cuesta el doble de espacio
    para los datos y algo más de escritura. Los metadatos ya están en DUP por defecto. No sustituye a la copia
    externa: si el disco entero falla, se pierden las dos copias."""
    if montaje("/", runner).fstype != "btrfs":
        raise CopiasError("la raíz no es btrfs")
    prof, used, free = perfil_datos(runner)
    target = "dup" if activar else "single"
    if prof == target:
        return f"Ya está en {target}."
    if activar and free < used * 1.1:
        raise CopiasError(f"no hay espacio: los datos ocupan {used >> 30} GiB y quedan {free >> 30} GiB libres "
                          "(hace falta otro tanto)")
    runner(["btrfs", "balance", "start", f"-dconvert={target}" + (",soft" if activar else ""), "/"])
    return ("Autorreparación activada: cada dato tiene dos copias." if activar
            else "Autorreparación desactivada: datos en una sola copia.")


# ---------------------------------------------------------------- copia externa (restic)
def config(path: Path = CONFIG) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _restic_env(dest: str, keyfile: Path) -> dict:
    return {"PATH": "/usr/bin:/bin", "HOME": "/root", "LANG": "C.UTF-8", "RESTIC_REPOSITORY": dest,
            "RESTIC_PASSWORD_FILE": str(keyfile), "RESTIC_CACHE_DIR": "/var/cache/tisma-copias"}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def configurar_externo(destino: str, runner=run, cfg: Path = CONFIG, keyfile: Path = KEYFILE) -> tuple[str, bool]:
    """Prepara el repositorio cifrado. Devuelve (clave, misma_unidad). La clave se muestra UNA vez: sin ella
    la copia no se puede leer en otro equipo."""
    if not destino.startswith("/") or "\n" in destino:
        raise CopiasError("indica la ruta absoluta del destino (p. ej. /media/usuario/DISCO/tisma)")
    dest = Path(destino)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Mismo dispositivo de bloque (no st_dev: cada subvolumen btrfs tiene uno distinto aunque sea el mismo disco)
    try:
        same = montaje(str(dest), runner).source == montaje("/", runner).source
    except CopiasError:
        same = os.stat(dest).st_dev == os.stat("/").st_dev
    if keyfile.exists():
        clave = keyfile.read_text().strip()
    else:
        import secrets
        clave = "-".join(secrets.token_hex(3) for _ in range(6))
        _write_private(keyfile, clave + "\n")
    env = _restic_env(destino, keyfile)
    if not (dest / "config").exists():
        runner(["restic", "init"], env=env)
    _write_private(cfg, json.dumps({"destino": destino, "rutas": ["/home", "/etc"], "excluir": EXCLUIR},
                                   indent=2))
    return clave, same


def copiar_externo(runner=run, cfg: Path = CONFIG, keyfile: Path = KEYFILE) -> str:
    c = config(cfg)
    if not c.get("destino"):
        return "Copia externa no configurada (tisma-copias externo configurar <destino>)."
    if not (Path(c["destino"]) / "config").exists():
        return f"El destino {c['destino']} no está conectado: se reintentará en la próxima ejecución."
    env = _restic_env(c["destino"], keyfile)
    args = ["restic", "backup", "--exclude-caches", "--tag", "tisma", *c.get("rutas", ["/home", "/etc"])]
    for e in c.get("excluir", EXCLUIR):
        args += ["--exclude", e]
    args += ["--exclude", c["destino"]]         # si el destino está dentro de /home, no copiarlo dentro de sí mismo
    runner(args, env=env)
    runner(["restic", "forget", "--tag", "tisma", "--keep-daily", "7", "--keep-weekly", "4",
            "--keep-monthly", "6", "--prune"], env=env)
    return f"Copia externa hecha en {c['destino']}."


def comprobar_externo(runner=run, cfg: Path = CONFIG, keyfile: Path = KEYFILE) -> str:
    c = config(cfg)
    if not c.get("destino") or not (Path(c["destino"]) / "config").exists():
        return "Destino no configurado o no conectado."
    r = runner(["restic", "check", "--read-data-subset=5%"], env=_restic_env(c["destino"], keyfile))
    return r.stdout.strip()[-400:] or "Repositorio correcto."


def restaurar_externo(snapshot_id: str, destino: str, runner=run, cfg: Path = CONFIG,
                      keyfile: Path = KEYFILE) -> str:
    """Restaura en una carpeta NUEVA (nunca sobrescribe): desde ahí se copian los archivos que hagan falta."""
    if not re.fullmatch(r"[0-9a-f]{8,64}|latest", snapshot_id):
        raise CopiasError("identificador de copia no válido")
    target = Path(destino)
    if target.exists() and any(target.iterdir()):
        raise CopiasError("el destino de la restauración debe estar vacío")
    c = config(cfg)
    if not c.get("destino"):
        raise CopiasError("la copia externa no está configurada")
    if not target.is_absolute():
        raise CopiasError("indica la ruta completa del destino de la restauración")
    runner(["restic", "restore", snapshot_id, "--target", str(target)], env=_restic_env(c["destino"], keyfile))
    return str(target)


def estado(runner=run) -> dict:
    out = {"instantaneas": instantaneas(), "externo": {}}
    try:
        m = montaje("/", runner)
        out["raiz"] = {"fstype": m.fstype, "subvol": m.subvol}
    except CopiasError:
        out["raiz"] = {}
    c = config()
    if c.get("destino"):
        out["externo"] = {"destino": c["destino"], "conectado": (Path(c["destino"]) / "config").exists()}
    out["btrfs"] = shutil.which("btrfs") is not None
    return out
