"""Preferencias TISMA: lo que una persona cambia de verdad, en una sola ventana y en simple.

Aspecto (claro/oscuro y color), tamaño del texto, fondo de pantalla, modo de energía (Eco · Equilibrado · Turbo),
cuándo se apaga y bloquea la pantalla, e idioma del teclado. Lo demás, en «Opciones avanzadas» (Configuración del
sistema, TISMA Rendimiento, TISMA Seguridad).

Igual que las otras apps de TISMA: servidor en 127.0.0.1 con token y Host verificado, y SOLO órdenes fijas con
valores de listas cerradas (nada de lo que llega por HTTP se pasa a un intérprete). Todo es del usuario, salvo el
modo de energía, que polkit ya autoriza para la sesión.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

for _p in ("/usr/lib/centinela/python", str(Path(__file__).resolve().parents[2] / "bienvenida"),
           str(Path(__file__).resolve().parents[2] / "centinela")):
    if _p not in sys.path:
        sys.path.append(_p)

STATIC = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)
BOOT = secrets.token_urlsafe(24)
BOOT_USED = threading.Event()
RUN = subprocess.run
POPEN = subprocess.Popen
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONF = CONFIG / "tisma" / "preferencias.json"
FONDOS = Path(os.environ.get("TISMA_FONDOS", "/usr/share/wallpapers"))
IMAGENES = (".jpg", ".jpeg", ".png", ".webp")

TAMANOS = {"normal": 10, "grande": 12, "muy_grande": 14}
FUENTES = {"font": "Inter", "menuFont": "Inter", "toolBarFont": "Inter", "fixed": "JetBrains Mono"}
ENERGIA = ("silencioso", "equilibrado", "rendimiento")
PANTALLA = (5, 10, 15, 30, 0)                  # minutos; 0 = nunca
TECLADOS = {"latam": "Español (Latinoamérica)", "es": "Español (España)", "us": "Inglés (EE. UU.)",
            "br": "Portugués (Brasil)"}
AVANZADO = {"sistema": ["systemsettings"], "rendimiento": ["tisma-rendimiento"],
            "seguridad": ["tisma-seguridad-panel"]}


def _ok(argv: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        r = RUN(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stderr or r.stdout or "")[-300:]
    except FileNotFoundError:
        return False, f"falta «{argv[0]}»"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def kwrite(archivo: str, grupos: list[str], clave: str, valor: str) -> tuple[bool, str]:
    exe = shutil.which("kwriteconfig6") or "kwriteconfig6"
    argv = [exe, "--file", archivo]
    for g in grupos:
        argv += ["--group", g]
    return _ok(argv + ["--key", clave, valor])


def leer_ini(archivo: str, grupo: str, clave: str) -> str | None:
    """Valor de un .ini de KDE (el del usuario manda sobre el de /etc/xdg)."""
    for f in (CONFIG / archivo, Path("/etc/xdg") / archivo):
        try:
            actual = None
            for linea in f.read_text().splitlines():
                linea = linea.strip()
                if linea.startswith("["):
                    actual = linea
                elif actual == f"[{grupo}]" and linea.startswith(clave + "="):
                    return linea.split("=", 1)[1]
        except OSError:
            continue
    return None


def leer_conf() -> dict:
    try:
        return json.loads(CONF.read_text())
    except (OSError, ValueError):
        return {}


def guardar_conf(**kv) -> None:
    c = leer_conf()
    c.update(kv)
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(c))


def avisar_cambio_kde(tipo: int) -> None:
    """KGlobalSettings: 1 = fuentes. Las apps abiertas lo toman; las demás, al abrirse."""
    if shutil.which("dbus-send"):
        _ok(["dbus-send", "--session", "--type=signal", "/KGlobalSettings", "org.kde.KGlobalSettings.notifyChange",
             f"int32:{tipo}", "int32:0"])


# ---------------------------------------------------------------- ajustes
def tamano_actual() -> str:
    v = leer_ini("kdeglobals", "General", "font") or ""
    m = re.match(r"[^,]+,(\d+)", v)
    n = int(m.group(1)) if m else 10
    return min(TAMANOS, key=lambda k: abs(TAMANOS[k] - n))


def poner_tamano(t: str) -> dict:
    n = TAMANOS[t]
    errores = []
    for clave, familia in FUENTES.items():
        ok, out = kwrite("kdeglobals", ["General"], clave, f"{familia},{n},-1,5,400,0,0,0,0,0,0,0,0,0,0,1")
        if not ok:
            errores.append(out)
    kwrite("kdeglobals", ["General"], "smallestReadableFont", f"Inter,{n - 1},-1,5,400,0,0,0,0,0,0,0,0,0,0,1")
    avisar_cambio_kde(1)
    return {"ok": not errores, "salida": "; ".join(dict.fromkeys(errores)),
            "nota": "Las ventanas que abras desde ahora ya lo usan; las abiertas, al volver a abrirlas."}


def fondos() -> list[dict]:
    """Fondos instalados (paquetes de /usr/share/wallpapers) con su miniatura."""
    out = []
    try:
        dirs = sorted(d for d in FONDOS.iterdir() if d.is_dir() and re.fullmatch(r"[\w.-]+", d.name))
    except OSError:
        return []
    for d in dirs:
        if miniatura(d.name):
            nombre = d.name
            try:
                meta = json.loads((d / "metadata.json").read_text())
                nombre = meta.get("KPlugin", {}).get("Name", nombre)
            except (OSError, ValueError):
                pass
            out.append({"id": d.name, "nombre": nombre})
    return out


def miniatura(id_: str) -> Path | None:
    if not re.fullmatch(r"[\w.-]+", id_ or ""):
        return None
    d = FONDOS / id_
    for cand in [d / "contents" / "screenshot.png", *sorted((d / "contents" / "images").glob("*"))]:
        if cand.suffix.lower() in IMAGENES and cand.is_file():
            return cand
    return None


def poner_fondo(ruta: Path) -> dict:
    ok, out = _ok(["plasma-apply-wallpaperimage", str(ruta)])
    uri = ruta.as_uri()
    # La pantalla de bloqueo con el mismo fondo
    kwrite("kscreenlockerrc", ["Greeter", "Wallpaper", "org.kde.image", "General"], "Image", uri)
    if ok:
        guardar_conf(fondo=str(ruta))
    return {"ok": ok, "salida": out}


def elegir_foto() -> dict:
    """Una foto propia, elegida con la ventana de archivos del sistema (la ruta no la escribe la página)."""
    if not shutil.which("kdialog"):
        return {"ok": False, "salida": "falta el selector de archivos"}
    inicio = next((str(p) for p in (Path.home() / "Imágenes", Path.home() / "Pictures") if p.is_dir()),
                  str(Path.home()))
    try:
        r = RUN(["kdialog", "--title", "Elige una foto para el fondo", "--getopenfilename", inicio,
                 "image/jpeg image/png image/webp"], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "salida": str(e)}
    elegido = (r.stdout or "").strip()
    if r.returncode != 0 or not elegido:
        return {"ok": False, "cancelado": True}
    p = Path(elegido).resolve()
    if not p.is_file() or p.suffix.lower() not in IMAGENES:
        return {"ok": False, "salida": "Elige una imagen JPG, PNG o WEBP"}
    return poner_fondo(p)


def energia_actual() -> str:
    try:
        p = json.loads(Path("/var/lib/centinela/prefs.json").read_text()).get("perfil")
        if p in ENERGIA:
            return p
    except (OSError, ValueError):
        pass
    return leer_conf().get("energia", "equilibrado")


def poner_energia(p: str) -> dict:
    ok, out = _ok(["systemctl", "start", f"centinela-perfil@{p}.service"], timeout=60)
    if ok:
        guardar_conf(energia=p)
    return {"ok": ok, "salida": out}


def pantalla_actual() -> int:
    if (leer_ini("kscreenlockerrc", "Daemon", "Autolock") or "true") == "false":
        return 0
    try:
        return int(leer_ini("kscreenlockerrc", "Daemon", "Timeout") or 5)
    except ValueError:
        return 5


def poner_pantalla(minutos: int) -> dict:
    nunca = minutos == 0
    kwrite("kscreenlockerrc", ["Daemon"], "Autolock", "false" if nunca else "true")
    if not nunca:
        kwrite("kscreenlockerrc", ["Daemon"], "Timeout", str(minutos))
    for modo in ("AC", "Battery", "LowBattery"):
        kwrite("powerdevilrc", [modo, "Display"], "TurnOffDisplayWhenIdle", "false" if nunca else "true")
        if not nunca:
            # A batería, como mucho 5 min: la pantalla es lo que más gasta
            seg = min(minutos, 5) * 60 if modo != "AC" else minutos * 60
            kwrite("powerdevilrc", [modo, "Display"], "TurnOffDisplayIdleTimeoutSec", str(seg))
    if shutil.which("dbus-send"):
        _ok(["dbus-send", "--session", "--type=method_call", "--dest=org.kde.Solid.PowerManagement",
             "/org/kde/Solid/PowerManagement", "org.kde.Solid.PowerManagement.reparseConfiguration"])
        _ok(["dbus-send", "--session", "--type=method_call", "--dest=org.freedesktop.ScreenSaver", "/ScreenSaver",
             "org.kde.screensaver.configure"])
    guardar_conf(pantalla=minutos)
    return {"ok": True, "nota": "" if not nunca else
            "Sin bloqueo automático: cualquiera que use el equipo puede ver tus cosas si te alejas."}


def teclado_actual() -> str:
    v = (leer_ini("kxkbrc", "Layout", "LayoutList") or "").split(",")[0]
    return v if v in TECLADOS else "latam"


def poner_teclado(t: str) -> dict:
    kwrite("kxkbrc", ["Layout"], "Use", "true")
    ok, out = kwrite("kxkbrc", ["Layout"], "LayoutList", t)
    if shutil.which("dbus-send"):
        _ok(["dbus-send", "--session", "--type=signal", "/Layouts", "org.kde.keyboard.reloadConfig"])
    return {"ok": ok, "salida": out}


def estado() -> dict:
    from tisma_bienvenida.server import ACENTOS, read_conf
    b = read_conf()
    return {"tema": b.get("tema", "oscuro"), "acento": b.get("acento", "violeta"), "acentos": ACENTOS,
            "tamano": tamano_actual(), "energia": energia_actual(), "pantalla": pantalla_actual(),
            "teclado": teclado_actual(), "teclados": TECLADOS, "fondos": fondos(),
            "bateria": any(Path(p, "type").read_text().strip() == "Battery"
                           for p in Path("/sys/class/power_supply").glob("*") if Path(p, "type").is_file())}


# ---------------------------------------------------------------- servidor
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if not ctype.startswith("image/") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if getattr(self, "_cookie", None):
                self.send_header("Set-Cookie", self._cookie)
                self._cookie = None
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _cookie_ok(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "tp" and secrets.compare_digest(v, TOKEN):
                return True
        return False

    def _auth(self):
        return self._host_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        if u.path == "/":
            boot = secrets.compare_digest(q.get("t", ""), BOOT) and not BOOT_USED.is_set()
            if not (boot or self._cookie_ok()):
                return self._send(403, b"Abre Preferencias desde su lanzador.", "text/plain")
            BOOT_USED.set()
            self._cookie = f"tp={TOKEN}; HttpOnly; SameSite=Strict; Path=/"
            return self._send(200, (STATIC / "index.html").read_text().replace("__TOKEN__", TOKEN).encode(),
                              "text/html")
        if u.path == "/static/logo.svg":
            return self._send(200, (STATIC / "logo.svg").read_bytes(), "image/svg+xml")
        if u.path == "/miniatura":                    # <img>: con la cookie (no puede llevar cabeceras)
            if not self._cookie_ok():
                return self._send(403, {"error": "no autorizado"})
            m = miniatura(q.get("id", ""))
            if not m:
                return self._send(404, {"error": "no encontrado"})
            tipo = {".png": "image/png", ".webp": "image/webp"}.get(m.suffix.lower(), "image/jpeg")
            return self._send(200, m.read_bytes(), tipo)
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        if u.path == "/api/estado":
            return self._send(200, estado())
        return self._send(404, {"error": "no encontrado"})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            b = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "JSON inválido"})
        u = urlparse(self.path).path
        if u == "/api/tema":
            from tisma_bienvenida.server import ACENTOS, TEMAS, apply_theme
            if b.get("modo") not in TEMAS or b.get("acento") not in ACENTOS:
                return self._send(400, {"error": "tema o acento inválido"})
            return self._send(200, apply_theme(b["modo"], b["acento"]))
        if u == "/api/tamano" and b.get("valor") in TAMANOS:
            return self._send(200, poner_tamano(b["valor"]))
        if u == "/api/fondo":
            if b.get("propio") is True:
                return self._send(200, elegir_foto())
            m = miniatura(str(b.get("id") or ""))
            if not m:
                return self._send(400, {"error": "fondo desconocido"})
            return self._send(200, poner_fondo(FONDOS / b["id"]))
        if u == "/api/energia" and b.get("valor") in ENERGIA:
            return self._send(200, poner_energia(b["valor"]))
        if u == "/api/pantalla" and b.get("valor") in PANTALLA:
            return self._send(200, poner_pantalla(b["valor"]))
        if u == "/api/teclado" and b.get("valor") in TECLADOS:
            return self._send(200, poner_teclado(b["valor"]))
        if u == "/api/abrir":
            argv = AVANZADO.get(b.get("app"))
            if not argv or not shutil.which(argv[0]):
                return self._send(400, {"error": "no disponible"})
            POPEN(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return self._send(200, {"ok": True})
        return self._send(400, {"error": "pedido inválido"})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tisma-preferencias")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{httpd.server_port}/?t={BOOT}"
    print(f"Preferencias TISMA en el puerto {httpd.server_port}", flush=True)
    if a.no_browser:
        print(url, flush=True)
    else:
        try:
            from centinela.ventana import abrir_ventana
            abrir_ventana(url, "tisma-preferencias", "900,720",
                          al_cerrar=lambda: threading.Thread(target=httpd.shutdown, daemon=True).start())
        except ImportError:
            import webbrowser
            webbrowser.open(url)
    t = threading.Timer(4 * 3600, httpd.shutdown)
    t.daemon = True
    t.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
