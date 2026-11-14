"""Guía TISMA: tutorial del menú y de cada función, para que cualquiera pueda usar el equipo.

Lecciones cortas en pasos (lecciones.py), con imágenes reales y un botón «Probar» que abre esa función. El botón solo
puede abrir cosas de una LISTA FIJA (nunca una orden que mande la página). Sin Internet. Servidor solo en
127.0.0.1, con token y Host verificado, como el resto de apps de TISMA.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .lecciones import CATEGORIAS, LECCIONES

STATIC = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)
BOOT = secrets.token_urlsafe(24)
BOOT_USED = threading.Event()
POPEN = subprocess.Popen

PLASMA = ["gdbus", "call", "--session", "--dest", "org.kde.plasmashell", "--object-path", "/PlasmaShell", "--method"]
PROBAR = {
    "menu": PLASMA + ["org.kde.PlasmaShell.activateLauncherMenu"],
    "archivos": ["dolphin"],
    "wifi": ["systemsettings", "kcm_networkmanagement"],
    "sonido": ["systemsettings", "kcm_pulseaudio"],
    "impresoras": ["systemsettings", "kcm_printer_manager"],
    "tienda": ["plasma-discover"],
    "asistente": ["tisma-asistente"],
    "seguridad": ["tisma-seguridad-panel"],
    "bienvenida": ["tisma-bienvenida"],
    "copias": ["tisma-copias-panel"],
    "preferencias": ["tisma-preferencias"],
}


def normalizar(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def buscar(q: str) -> list[str]:
    """Ids de lecciones que contienen todas las palabras de la búsqueda (sin tildes ni mayúsculas)."""
    palabras = [p for p in re.split(r"\W+", normalizar(q)) if len(p) > 1]
    if not palabras:
        return [l["id"] for l in LECCIONES]
    out = []
    for l in LECCIONES:
        texto = normalizar(" ".join([l["titulo"], l["resumen"], l["palabras"], *l["pasos"]]))
        if all(p in texto for p in palabras):
            out.append(l["id"])
    return out


def probar(accion: str) -> dict:
    argv = PROBAR.get(accion)
    if not argv:
        return {"ok": False, "error": "no disponible"}
    if not shutil.which(argv[0]):
        return {"ok": False, "error": "Esa función no está instalada en este equipo"}
    POPEN(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
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

    def _auth(self):
        return self._host_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def _cookie_ok(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "tg" and secrets.compare_digest(v, TOKEN):
                return True
        return False

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        if u.path == "/":
            boot = secrets.compare_digest(q.get("t", ""), BOOT) and not BOOT_USED.is_set()
            if not (boot or self._cookie_ok()):
                return self._send(403, "Abre la Guía TISMA desde su lanzador.".encode(), "text/plain")
            BOOT_USED.set()
            self._cookie = f"tg={TOKEN}; HttpOnly; SameSite=Strict; Path=/"
            return self._send(200, (STATIC / "index.html").read_text().replace("__TOKEN__", TOKEN).encode(), "text/html")
        if u.path == "/static/logo.svg":
            return self._send(200, (STATIC / "logo.svg").read_bytes(), "image/svg+xml")
        m = re.fullmatch(r"/img/([a-z0-9-]{1,40}\.png)", u.path)
        if m:
            f = STATIC / "img" / m.group(1)
            return self._send(200, f.read_bytes(), "image/png") if f.is_file() else self._send(404, {"error": "no"})
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        if u.path == "/api/lecciones":
            hay = {p.name for p in (STATIC / "img").glob("*.png")}
            return self._send(200, {"categorias": CATEGORIAS, "lecciones": [
                {**l, "imagen": l["imagen"] if l["imagen"] in hay else None,
                 "probar": l["probar"] if l["probar"] and shutil.which(PROBAR[l["probar"]][0]) else None}
                for l in LECCIONES]})
        if u.path == "/api/buscar":
            return self._send(200, {"ids": buscar(q.get("q", "")[:100])})
        return self._send(404, {"error": "no encontrado"})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            b = json.loads(self.rfile.read(min(n, 2000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "JSON inválido"})
        if urlparse(self.path).path == "/api/probar" and isinstance(b, dict):
            r = probar(str(b.get("accion") or ""))
            return self._send(200 if r["ok"] else 404, r)
        return self._send(404, {"error": "no encontrado"})


def abrir(url: str, httpd) -> None:
    try:
        import sys
        sys.path.insert(0, "/usr/lib/centinela/python")
        from centinela.ventana import abrir_ventana
    except ImportError:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return
    abrir_ventana(url, "tisma-guia", "1120,780",
                  al_cerrar=lambda: threading.Thread(target=httpd.shutdown, daemon=True).start())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tisma-guia")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{httpd.server_port}/?t={BOOT}"
    if a.no_browser:
        print(url, flush=True)
    else:
        abrir(url, httpd)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
