"""Ventana «Actualizar TISMA» de la sesión del USB: si el equipo ya tiene TISMA, se ofrece actualizarlo
(conservando todo) antes que instalar de cero. Botones y una barra de progreso; nada de terminal.

La página solo puede pedir tres cosas fijas: actualizar, volver a la versión anterior o abrir el instalador. La
contraseña del disco va por la entrada estándar del proceso (nunca en la línea de órdenes ni en registros).
Servidor solo en 127.0.0.1, con token y Host verificado, como el resto de apps de TISMA.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import motor

STATIC = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)
BOOT = secrets.token_urlsafe(24)
BOOT_USED = threading.Event()
POPEN = subprocess.Popen
MOTOR = "/usr/lib/tisma/actualizar-instalado"
INSTALADOR = "/usr/lib/tisma/instalar-tisma"
LOCK = threading.Lock()
ESTADO: dict = {"corriendo": False, "pct": 0, "texto": "", "error": None, "listo": None}


def leer_progreso(proc) -> None:
    for linea in proc.stdout:
        try:
            d = json.loads(linea)
        except ValueError:
            continue
        with LOCK:
            if "error" in d:
                ESTADO["error"] = d["error"]
            elif d.get("paso") == "listo":
                ESTADO.update(listo=d.get("texto"), pct=100)
            else:
                ESTADO.update(texto=d.get("texto") or ESTADO["texto"],
                              pct=d["pct"] if isinstance(d.get("pct"), int) else ESTADO["pct"])
    proc.wait()
    with LOCK:
        ESTADO["corriendo"] = False
        if proc.returncode != 0 and not ESTADO["error"]:
            ESTADO["error"] = "No se pudo terminar. Tu sistema quedó como estaba."


def lanzar(orden: str, raiz: str, clave: str) -> dict:
    if orden not in ("actualizar", "volver"):
        return {"ok": False, "error": "acción desconocida"}
    if raiz not in {d["raiz"] for d in motor.buscar()}:
        return {"ok": False, "error": "No encontré TISMA instalado en ese disco."}
    if "\n" in clave or len(clave) > 512:
        return {"ok": False, "error": "contraseña no válida"}
    with LOCK:
        if ESTADO["corriendo"]:
            return {"ok": False, "error": "Ya hay una actualización en curso."}
        ESTADO.update(corriendo=True, pct=1, texto="Empezando…", error=None, listo=None)
    proc = POPEN(["pkexec", MOTOR, orden, raiz], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                 stderr=subprocess.DEVNULL, text=True, bufsize=1)
    proc.stdin.write(clave + "\n")
    proc.stdin.close()
    threading.Thread(target=leer_progreso, args=(proc,), daemon=True).start()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
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
            if k == "ta" and secrets.compare_digest(v, TOKEN):
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
                return self._send(403, "Abre «Actualizar TISMA» desde su icono.".encode(), "text/plain")
            BOOT_USED.set()
            self._cookie = f"ta={TOKEN}; HttpOnly; SameSite=Strict; Path=/"
            return self._send(200, (STATIC / "index.html").read_text().replace("__TOKEN__", TOKEN).encode(),
                              "text/html")
        if u.path == "/static/logo.svg":
            return self._send(200, (STATIC / "logo.svg").read_bytes(), "image/svg+xml")
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        if u.path == "/api/discos":
            return self._send(200, {"discos": motor.buscar()})
        if u.path == "/api/estado":
            with LOCK:
                return self._send(200, dict(ESTADO))
        return self._send(404, {"error": "no encontrado"})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            b = json.loads(self.rfile.read(min(n, 4000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "JSON inválido"})
        ruta = urlparse(self.path).path
        if ruta == "/api/hacer" and isinstance(b, dict):
            r = lanzar(str(b.get("orden") or ""), str(b.get("raiz") or ""), str(b.get("clave") or ""))
            return self._send(200 if r["ok"] else 409, r)
        if ruta == "/api/instalar":
            POPEN([INSTALADOR], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._send(200, {"ok": True})
        if ruta == "/api/reiniciar":
            POPEN(["systemctl", "reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "no encontrado"})


def abrir(url: str, httpd) -> None:
    try:
        import sys
        sys.path.insert(0, "/usr/lib/centinela/python")
        from centinela.ventana import abrir_ventana
    except ImportError:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return
    abrir_ventana(url, "tisma-actualizar", "900,640",
                  al_cerrar=lambda: threading.Thread(target=httpd.shutdown, daemon=True).start())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tisma-actualizar-usb")
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
