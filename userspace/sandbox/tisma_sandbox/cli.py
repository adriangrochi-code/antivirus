"""tisma-sandbox: ejecuta aplicaciones en su propio sandbox.

  tisma-sandbox lista                       perfiles disponibles
  tisma-sandbox ver <perfil>                permisos del perfil
  tisma-sandbox ejecutar <perfil> [args…]   lanza la app aislada
  tisma-sandbox instalar <perfil>           el lanzador del menú abre la app siempre aislada
  tisma-sandbox quitar <perfil>             deshace «instalar»
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import profile as prof

APPS_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"


def start_dbus_proxy(p: prof.Profile, tmp: str) -> tuple[subprocess.Popen | None, str | None]:
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    proxy = shutil.which("xdg-dbus-proxy")
    if not (bus and proxy):
        return None, None                      # sin D-Bus (más restrictivo, nunca más permisivo)
    sock = os.path.join(tmp, "bus")
    proc = subprocess.Popen([proxy, bus, sock, *prof.dbus_proxy_args(p)])
    for _ in range(50):
        if os.path.exists(sock):
            return proc, sock
        time.sleep(0.05)
    proc.terminate()
    return None, None


def run(p: prof.Profile, argv: list[str]) -> int:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        print("error: bubblewrap no está instalado; no se ejecuta sin aislamiento", file=sys.stderr)
        return 1
    app_home = prof.DATA_DIR / p.nombre
    app_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=f"tisma-sb-{p.nombre}-") as tmp:
        proxy, sock = start_dbus_proxy(p, tmp)
        try:
            args = prof.bwrap_args(p, uid=os.getuid(), runtime_dir=f"/run/user/{os.getuid()}",
                                   app_home=app_home, dbus_socket=sock)
            return subprocess.call([bwrap, *args, "--", p.exec, *argv])
        finally:
            if proxy:
                proxy.terminate()


def describe(p: prof.Profile) -> str:
    yes = lambda b: "sí" if b else "no"  # noqa: E731
    return "\n".join([
        f"{p.nombre}: {p.descripcion}", f"  ejecutable: {p.exec}",
        f"  red: {yes(p.red)}  GPU: {yes(p.gpu)}  audio: {yes(p.audio)}  cámara: {yes(p.camara)}  mandos: {yes(p.mandos)}",
        f"  escritura: {', '.join(p.rw) or '—'}", f"  solo lectura: {', '.join(p.ro) or '—'}",
        f"  D-Bus: {', '.join(p.dbus_talk + ['portales'] * p.portales) or '—'}",
        f"  datos privados de la app: {prof.DATA_DIR / p.nombre}"])


def install(p: prof.Profile) -> Path:
    if not p.escritorio:
        raise prof.ProfileError("El perfil no indica qué lanzador reemplazar (campo «escritorio»)")
    src = next((d / f"{p.escritorio}.desktop" for d in (Path("/usr/share/applications"),)
                if (d / f"{p.escritorio}.desktop").exists()), None)
    lines = src.read_text().splitlines() if src else ["[Desktop Entry]", "Type=Application", f"Name={p.nombre}"]
    out, in_main = [], False
    for line in lines:
        if line.startswith("["):
            in_main = line.strip() == "[Desktop Entry]"
        if line.startswith(("Exec=", "TryExec=", "DBusActivatable=")):
            if line.startswith("Exec="):
                rest = line[5:].split(" ", 1)
                out.append(f"Exec=tisma-sandbox ejecutar {p.nombre}" + (f" {rest[1]}" if len(rest) > 1 else ""))
            continue
        out.append(line)
    out.append("X-TISMA-Sandbox=" + p.nombre)
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    dest = APPS_DIR / f"{p.escritorio}.desktop"
    dest.write_text("\n".join(out) + "\n")
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tisma-sandbox", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lista")
    for n in ("ver", "instalar", "quitar"):
        sub.add_parser(n).add_argument("perfil")
    e = sub.add_parser("ejecutar")
    e.add_argument("perfil")
    e.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "lista":
            for n in prof.available():
                print(n)
            return 0
        p = prof.load(a.perfil)
        if a.cmd == "ver":
            print(describe(p))
        elif a.cmd == "ejecutar":
            return run(p, a.args)
        elif a.cmd == "instalar":
            print(f"Lanzador aislado: {install(p)}")
        elif a.cmd == "quitar":
            (APPS_DIR / f"{p.escritorio}.desktop").unlink(missing_ok=True)
            print("Lanzador original restaurado")
    except prof.ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
