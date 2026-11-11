"""Servidor local de TISMA Rendimiento (127.0.0.1, token, Host verificado).

Lectura: métricas de centinela.status (sin privilegios), plan y mediciones de /etc y /var/lib.
Acciones: perfil de potencia (systemd + polkit, sin contraseña) y overclock (pkexec centinela oc,
con contraseña de administrador). Los argumentos se validan aquí y otra vez en centinela.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

for p in ("/usr/lib/centinela/python", str(Path(__file__).resolve().parents[2] / "centinela")):
    if p not in sys.path:
        sys.path.insert(0, p)
from centinela import status  # noqa: E402

STATIC = Path(__file__).parent / "static"
PROFILES = ("silencioso", "equilibrado", "rendimiento")
OC_MAX = 15
STATE = Path(os.environ.get("CENTINELA_STATE", "/var/lib/centinela"))
PLAN = Path(os.environ.get("CENTINELA_ETC", "/etc")) / "centinela/plan.json"
TOKEN = secrets.token_urlsafe(24)
RUN = subprocess.run          # punto de sustitución en las pruebas
PROGRESS = Path(os.environ.get("CENTINELA_PROGRESS", "/run/centinela/autotune.json"))
AUTOTUNE = {"proc": None}
POPEN = subprocess.Popen


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def device_info() -> dict:
    hw = read_json(STATE / "hardware.json")
    bench = read_json(STATE / "bench.json")
    plan = read_json(PLAN)
    cpu = hw.get("cpu", {})
    return {"producto": hw.get("product") or "", "cpu": cpu.get("model"), "nucleos": cpu.get("physical_cores"),
            "hilos": cpu.get("logical_cpus"), "modulos": hw.get("memory_modules", []),
            "ancho_banda": (bench.get("memory", {}) or {}).get("bandwidth_gbs"),
            "latencia": (bench.get("memory", {}) or {}).get("latency_ns"),
            "recomendaciones": plan.get("recommendations", []), "razones": plan.get("reasons", []),
            "rapl": plan.get("power", {}).get("rapl", {}),
            "bateria": (hw.get("power") or {}).get("battery", False),
            "umbral_carga": read_json(STATE / "prefs.json").get("umbral_carga")}


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

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        if u.path == "/":
            if not secrets.compare_digest(q.get("t", ""), TOKEN):
                return self._send(403, b"Abre TISMA Rendimiento desde su lanzador.", "text/plain")
            return self._send(200, (STATIC / "index.html").read_text().replace("__TOKEN__", TOKEN).encode(), "text/html")
        if u.path == "/static/logo.svg":
            return self._send(200, (STATIC / "logo.svg").read_bytes(), "image/svg+xml")
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        if u.path == "/api/estado":
            return self._send(200, status.snapshot())
        if u.path == "/api/equipo":
            return self._send(200, device_info())
        if u.path == "/api/autotune":
            p = AUTOTUNE["proc"]
            running = p is not None and p.poll() is None
            return self._send(200, {"en_curso": running, "progreso": read_json(PROGRESS),
                                    "codigo": None if running or p is None else p.returncode})
        if u.path == "/api/arranque":
            try:
                from centinela import boot
                return self._send(200, boot.analyze())
            except Exception as e:  # noqa: BLE001
                return self._send(200, {"error": str(e)})
        return self._send(404, {"error": "no encontrado"})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            b = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "JSON inválido"})
        u = urlparse(self.path)
        if u.path == "/api/perfil":
            perfil = b.get("perfil")
            if perfil not in PROFILES:
                return self._send(400, {"error": "perfil inválido"})
            r = RUN(["systemctl", "start", f"centinela-perfil@{perfil}.service"], capture_output=True, text=True, timeout=60)
            return self._send(200 if r.returncode == 0 else 500, {"ok": r.returncode == 0, "salida": (r.stderr or r.stdout)[-400:]})
        if u.path == "/api/autotune":
            seg = b.get("segundos", 45)
            if isinstance(seg, bool) or not isinstance(seg, int) or not 10 <= seg <= 600:
                return self._send(400, {"error": "segundos entre 10 y 600"})
            p = AUTOTUNE["proc"]
            if p is not None and p.poll() is None:
                return self._send(409, {"error": "ya hay una optimización en curso"})
            AUTOTUNE["proc"] = POPEN(["pkexec", "/usr/bin/centinela", "autotune", "--segundos", str(seg), "--json"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._send(200, {"ok": True})
        if u.path == "/api/umbral":
            on = b.get("activo")
            if on not in (True, False):
                return self._send(400, {"error": "activo debe ser verdadero o falso"})
            r = RUN(["pkexec", "/usr/bin/centinela", "energia", "umbral", "80" if on else "100"],
                    capture_output=True, text=True, timeout=120)
            return self._send(200 if r.returncode == 0 else 500, {"ok": r.returncode == 0,
                                                                   "salida": (r.stdout or r.stderr)[-400:]})
        if u.path == "/api/oc":
            argv = ["pkexec", "/usr/bin/centinela", "oc"]
            if "gpu" in b:
                gpu = b["gpu"]
                if isinstance(gpu, bool) or not isinstance(gpu, int) or not 0 <= gpu <= OC_MAX:
                    return self._send(400, {"error": f"overclock de GPU entre 0 y {OC_MAX} %"})
                argv += ["--gpu", str(gpu)]
            if "turbo" in b:
                if b["turbo"] not in (True, False):
                    return self._send(400, {"error": "turbo debe ser verdadero o falso"})
                argv += ["--turbo", "si" if b["turbo"] else "no"]
            if len(argv) == 3:
                return self._send(400, {"error": "nada que cambiar"})
            r = RUN(argv, capture_output=True, text=True, timeout=120)
            return self._send(200 if r.returncode == 0 else 500, {"ok": r.returncode == 0, "salida": (r.stderr or r.stdout)[-400:]})
        return self._send(404, {"error": "no encontrado"})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tisma-rendimiento")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{httpd.server_port}/?t={TOKEN}"
    print(f"TISMA Rendimiento en {url}", flush=True)
    if not a.no_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
