"""TISMA Seguridad: las herramientas de seguridad y mantenimiento con botones, sin terminal.

Cada botón lanza una tarea de una LISTA FIJA (la página nunca manda una orden): pkexec muestra la ventana de
contraseña del sistema y la tarea corre como administrador. Una tarea a la vez; la salida se muestra en vivo y al
terminar se resume en lenguaje simple (los detalles quedan a un clic). Servidor solo en 127.0.0.1, con token y
Host verificado, como el resto de apps de TISMA.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)
BOOT = secrets.token_urlsafe(24)
BOOT_USED = threading.Event()
POPEN = subprocess.Popen
RUN = subprocess.run
SEG = "/usr/bin/tisma-seguridad"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def carpeta_personal() -> str:
    return pwd.getpwuid(os.getuid()).pw_dir


# tarea: (título, orden, descripción). Las órdenes son fijas; las que llevan un dato lo validan en `orden()`.
TAREAS = {
    "virus": ("Buscar virus", lambda d: ["pkexec", SEG, "virus", carpeta_personal()],
              "Revisa tu carpeta personal con dos antivirus (Escudo y ClamAV)."),
    "rootkits": ("Buscar rootkits", lambda d: ["pkexec", SEG, "rootkits"],
                 "Busca programas ocultos que se esconden del sistema."),
    "auditoria": ("Revisar la configuración", lambda d: ["pkexec", SEG, "auditoria"],
                  "Comprueba que la configuración de seguridad del sistema esté bien."),
    "integridad": ("Comprobar archivos del sistema", lambda d: ["pkexec", SEG, "integridad"],
                   "Detecta si alguien cambió archivos del sistema (la primera vez prepara la referencia)."),
    "red": ("Ver la red", lambda d: ["pkexec", SEG, "red"],
            "Qué programas esperan conexiones y qué direcciones bloqueó Escudo."),
    "usb_cerrar": ("Permitir solo los USB de ahora", lambda d: ["pkexec", SEG, "usb", "--si"],
                   "Desde ahora se bloquea cualquier memoria, teclado o cable USB nuevo (protege de los USB "
                   "trucados). Los que tengas conectados ahora siguen funcionando."),
    "usb_permitir": ("Autorizar un USB", lambda d: ["pkexec", SEG, "usb-permitir", d["id"]]
                     if re.fullmatch(r"\d{1,6}", str(d.get("id", ""))) else None,
                     "Autoriza para siempre un USB que fue bloqueado."),
    "windows": ("Revisar el otro sistema del equipo", lambda d: ["pkexec", SEG, "windows"],
                "Busca virus y programas que arrancan solos en otro sistema instalado en este equipo (mientras está apagado)."),
    "windows_limpiar": ("Limpiar lo grave en el otro sistema", lambda d: ["pkexec", "/usr/bin/escudo", "windows", "--limpiar"],
                        "Desactiva lo peligroso (con copia del registro y cuarentena en C:\\TISMA-Cuarentena)."),
    "windows_kit": ("Copiar el kit de protección al otro sistema",
                    lambda d: ["pkexec", "/usr/bin/escudo", "windows", "--sin-archivos", "--kit"],
                    "Deja en C:\\TISMA-Kit un ajuste que cierra lo que usan los ataques (se puede deshacer)."),
    "estricto_on": ("Activar el modo estricto", lambda d: ["pkexec", SEG, "estricto", "on", "--si"],
                    "Más protección (como GrapheneOS). Algunas herramientas de programadores dejan de funcionar."),
    "estricto_off": ("Desactivar el modo estricto", lambda d: ["pkexec", SEG, "estricto", "off"],
                     "Vuelve a la protección normal."),
    "drivers": ("Buscar e instalar drivers", lambda d: ["pkexec", "/usr/bin/tisma-drivers", "instalar"],
                "Instala el firmware y los drivers que le falten a tu equipo."),
    "secureboot": ("Usar el kernel TISMA con Secure Boot", lambda d: ["pkexec", "/usr/bin/tisma-secureboot", "activar"],
                   "Registra la clave de TISMA: al reiniciar, una pantalla azul te pedirá una contraseña de un solo uso "
                   "que verás aquí."),
}
PELIGROSAS = {"usb_cerrar", "windows_limpiar", "estricto_on"}      # piden confirmación en la página
JOB = {"proc": None, "tarea": None, "salida": [], "inicio": 0.0, "codigo": None}
LOCK = threading.Lock()


def _leer(proc) -> None:
    for linea in iter(proc.stdout.readline, ""):
        with LOCK:
            JOB["salida"].append(ANSI.sub("", linea.rstrip("\n"))[:500])
            del JOB["salida"][:-4000]
    proc.wait()
    with LOCK:
        JOB["codigo"] = proc.returncode


def lanzar(tarea: str, datos: dict) -> dict:
    if tarea not in TAREAS:
        return {"ok": False, "error": "tarea desconocida"}
    argv = TAREAS[tarea][1](datos)
    if not argv:
        return {"ok": False, "error": "dato inválido"}
    with LOCK:
        p = JOB["proc"]
        if p is not None and p.poll() is None:
            return {"ok": False, "error": "Ya hay una tarea en curso: espera a que termine"}
        JOB.update(tarea=tarea, salida=[], inicio=time.time(), codigo=None)
        proc = POPEN(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                     bufsize=1, errors="replace", env={**os.environ, "LANG": os.environ.get("LANG", "es_ES.UTF-8")})
        JOB["proc"] = proc
    threading.Thread(target=_leer, args=(proc,), daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------- resumen en lenguaje simple
def resumir(tarea: str | None, salida: list[str], codigo: int | None) -> dict:
    """{"nivel": ok|aviso|error, "texto": …} a partir de la salida de la herramienta."""
    t = "\n".join(salida)
    if codigo == 126 or codigo == 127:
        return {"nivel": "error", "texto": "No se dio permiso de administrador: la tarea no se hizo."}
    if tarea == "virus":
        infectados = [ln for ln in salida if ln.rstrip().endswith("FOUND") or "MALICIOSO" in ln.upper()]
        if infectados:
            return {"nivel": "aviso", "texto": f"Se encontraron {len(infectados)} archivo(s) peligroso(s). "
                    "Escudo los puso en cuarentena o te avisó para decidir."}
        return {"nivel": "ok", "texto": "No se encontraron virus."}
    if tarea == "rootkits":
        avisos = [ln for ln in salida if re.search(r"\bWarning\b|INFECTED", ln)]
        return ({"nivel": "aviso", "texto": f"Hay {len(avisos)} punto(s) para revisar (suelen ser falsas alarmas "
                 "tras una actualización; los detalles están abajo)."} if avisos else
                {"nivel": "ok", "texto": "No se encontraron rootkits."})
    if tarea == "auditoria":
        m = re.search(r"Hardening index\s*:\s*(\d+)", t)
        n = int(m.group(1)) if m else None
        if n is None:
            return {"nivel": "aviso" if codigo else "ok", "texto": "Revisión terminada."}
        return {"nivel": "ok" if n >= 70 else "aviso",
                "texto": f"Nivel de protección de la configuración: {n} de 100" +
                         (" (muy bueno)." if n >= 80 else " (bueno)." if n >= 70 else ": hay recomendaciones abajo.")}
    if tarea == "integridad":
        if "base de referencia" in t:
            return {"nivel": "ok", "texto": "Referencia creada. Desde ahora se puede comprobar si algo cambia."}
        if "modificados" in t:
            return {"nivel": "aviso", "texto": "Hay archivos del sistema que cambiaron. Si no actualizaste el sistema "
                                               "hace poco, revisa los detalles."}
        return {"nivel": "ok", "texto": "Los archivos del sistema están como deben."}
    if tarea == "secureboot":
        m = re.search(r"contrase[ñn]a[^:]*:\s*(\S+)", t, re.I)
        return {"nivel": "ok" if codigo == 0 else "error",
                "texto": (f"Al reiniciar aparecerá una pantalla azul: elige «Enroll MOK», «Continue», «Yes» y escribe "
                          f"esta contraseña: {m.group(1)}" if m else
                          next((ln for ln in reversed(salida) if ln.strip()), "Listo."))
                if codigo == 0 else "No se pudo preparar. Revisa los detalles."}
    if codigo == 0:
        return {"nivel": "ok", "texto": "Listo."}
    return {"nivel": "error", "texto": "La tarea terminó con un problema. Los detalles están abajo."}


def usb_bloqueados() -> list[dict]:
    """Lista de USB bloqueados (sin contraseña si usbguard lo permite al usuario; si no, vacía)."""
    try:
        r = RUN(["usbguard", "list-devices", "--blocked"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for ln in r.stdout.splitlines():
        m = re.match(r'^(\d+): block id [0-9a-f:]+ .*name "([^"]*)"', ln)
        if m:
            out.append({"id": m.group(1), "nombre": m.group(2) or "Dispositivo USB"})
    return out


def informes() -> list[dict]:
    """Los programas que Escudo revisó para ESTE usuario (su carpeta privada), explicados en simple."""
    try:
        from escudo import informe
    except ImportError:
        return []
    return informe.leer(os.getuid())


# ---------------------------------------------------------------- HTTP
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
            if k == "ts" and secrets.compare_digest(v, TOKEN):
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
                return self._send(403, b"Abre TISMA Seguridad desde su lanzador.", "text/plain")
            BOOT_USED.set()
            self._cookie = f"ts={TOKEN}; HttpOnly; SameSite=Strict; Path=/"
            return self._send(200, (STATIC / "index.html").read_text().replace("__TOKEN__", TOKEN).encode(), "text/html")
        if u.path == "/static/logo.svg":
            return self._send(200, (STATIC / "logo.svg").read_bytes(), "image/svg+xml")
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        if u.path == "/api/tareas":
            return self._send(200, {k: {"titulo": v[0], "desc": v[2], "confirmar": k in PELIGROSAS}
                                    for k, v in TAREAS.items()})
        if u.path == "/api/tarea":
            with LOCK:
                p = JOB["proc"]
                corriendo = p is not None and p.poll() is None
                salida = list(JOB["salida"])
                d = {"tarea": JOB["tarea"], "corriendo": corriendo, "salida": salida[-400:],
                     "segundos": int(time.time() - JOB["inicio"]) if JOB["inicio"] else 0}
                if JOB["tarea"] and not corriendo and JOB["codigo"] is not None:
                    d["resumen"] = resumir(JOB["tarea"], salida, JOB["codigo"])
            return self._send(200, d)
        if u.path == "/api/usb":
            return self._send(200, {"bloqueados": usb_bloqueados()})
        if u.path == "/api/informes":
            return self._send(200, {"informes": informes()})
        return self._send(404, {"error": "no encontrado"})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            b = json.loads(self.rfile.read(min(n, 4000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "JSON inválido"})
        if urlparse(self.path).path == "/api/tarea" and isinstance(b, dict):
            r = lanzar(str(b.get("tarea") or ""), b)
            return self._send(200 if r["ok"] else 409, r)
        return self._send(404, {"error": "no encontrado"})


def abrir(url: str, httpd) -> None:
    try:
        import sys
        sys.path.insert(0, "/usr/lib/centinela/python")
        from centinela.ventana import abrir_ventana
    except ImportError:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return
    abrir_ventana(url, "tisma-seguridad", "1080,760",
                  al_cerrar=lambda: threading.Thread(target=httpd.shutdown, daemon=True).start())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tisma-seguridad-panel")
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
