"""Servidor local de TISMA Code (solo 127.0.0.1, protegido con token)."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import models
from .agent import Session
from .base import ToolError

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"/static/logo.svg": ("logo.svg", "image/svg+xml")}
MAX_LOG = 5000
CONFIG = Path.home() / ".config/tisma-code/config.json"
DEFAULT_ENDPOINT = "http://127.0.0.1:8080/v1"   # Strata


class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)      # de la API: solo dentro de la página y en una cookie HttpOnly
        # El de la URL va en el argv del navegador (cualquier usuario local lo lee en /proc): un solo uso
        self.boot = secrets.token_urlsafe(24)
        self.boot_used = threading.Event()
        self.session: Session | None = None
        self.log: list[dict] = []
        self.base = 0
        self.cond = threading.Condition()
        self.downloads = models.Downloads(on_progress=lambda d: self.push({"type": "download", **d}))
        self.server = models.LocalServer()
        self.hw = models.hardware()
        self.config = {"endpoint": DEFAULT_ENDPOINT, "model": "", "recent": [], "network": False}
        try:
            self.config.update(json.loads(CONFIG.read_text()))
        except (OSError, ValueError):
            pass

    def llm(self):
        """Cliente del motor con la clave de llama-server si el endpoint es el servidor propio."""
        import functools

        from .agent import chat_completion
        own = self.server.proc and self.config["endpoint"].endswith(f":{self.server.port}/v1")
        return functools.partial(chat_completion, api_key=self.server.api_key if own else "local")

    def motor_brief(self) -> dict:
        """Qué motor sirve ahora a TISMA Code y con qué configuración."""
        ep = self.config["endpoint"]
        own = self.server.proc and self.server.proc.poll() is None and ep.endswith(f":{self.server.port}/v1")
        if own:
            c = self.server.config or {}
            return {"nombre": "llama.cpp", "modelo": self.server.model, "detalle": c.get("motivo", ""),
                    "medido": bool(c.get("medido")), "gen": c.get("gen"), "contexto": self.server.ctx}
        if ep == DEFAULT_ENDPOINT:
            return {"nombre": "Strata", "modelo": self.config.get("model", ""), "detalle": "", "medido": False}
        return {"nombre": "externo", "modelo": self.config.get("model", ""), "detalle": ep, "medido": False}

    def auto_motor(self) -> None:
        """Al abrir TISMA Code: si no hay motor en marcha, el motor TISMA elige y arranca el adecuado."""
        try:
            self._auto_motor()
        except Exception as e:  # noqa: BLE001 — un modelo raro nunca debe dejar la app sin arrancar
            self.push({"type": "error", "message": f"El motor TISMA no pudo arrancar solo: {e}"})

    def _auto_motor(self) -> None:
        from . import motor
        if models.endpoint_models(self.config["endpoint"]):
            return
        strata_alive = bool(models.endpoint_models(DEFAULT_ENDPOINT))
        eng = motor.choose_engine(self.hw, strata_alive)
        if eng["motor"] == "strata":
            self.config["endpoint"] = DEFAULT_ENDPOINT
            self.push({"type": "info", "message": eng["motivo"]})
            return
        rec = motor.choose_model(models.local_models(), self.hw)
        if not rec:
            self.push({"type": "info", "message": f"{eng['motivo']}. Descarga un modelo (＋ Añadir modelo) y el motor "
                       "TISMA lo configurará para este equipo."})
            return
        try:
            ep = self.server.start(rec["path"], self.hw)
        except (OSError, RuntimeError, ValueError) as e:
            self.push({"type": "error", "message": f"No se pudo arrancar llama.cpp: {e}"})
            return
        self.config.update({"endpoint": ep, "model": Path(rec["path"]).name})
        self.save_config()
        if eng.get("instalar"):
            self.push({"type": "info", "message": eng["motivo"]})
        c = self.server.config or {}
        self.push({"type": "info", "message": f"Motor TISMA: llama.cpp con {Path(rec['path']).name} — "
                   f"{c.get('motivo', '')}" + ("" if c.get("medido") else
                                                ". Pulsa «⚙ optimizar» para medir la configuración más rápida.")})

    _optimizing = threading.Lock()

    def optimize(self, path: str) -> None:
        from . import motor
        if not self._optimizing.acquire(blocking=False):
            self.push({"type": "error", "message": "Ya hay una optimización en curso"})
            return
        try:
            self._optimize(path, motor)
        finally:
            self._optimizing.release()

    def _optimize(self, path: str, motor) -> None:
        self.push({"type": "info", "message": "Optimizando el motor para este modelo (unos minutos: se prueban "
                   "varias configuraciones)…"})
        was_serving = bool(self.server.proc and self.server.model == Path(path).name)
        if self.server.proc:
            self.server.stop()            # su memoria (VRAM incluida) falsearía las mediciones
        try:
            rec = motor.optimize(path, self.hw, on_step=lambda m: self.push({"type": "info", "message": "· " + m}))
        except Exception as e:  # noqa: BLE001 (GGUF raro, motor que no arranca…): se avisa y se restaura
            self.push({"type": "error", "message": f"No se pudo optimizar: {e}"})
            rec = {}
        if was_serving or not models.endpoint_models(self.config["endpoint"]):
            try:
                self.config["endpoint"] = self.server.start(path, self.hw)
                if self.session:
                    self.session.endpoint = self.config["endpoint"]
                    self.session.context_tokens = self.context(self.server.ctx)
            except (OSError, RuntimeError, ValueError) as e:
                self.push({"type": "error", "message": f"No se pudo volver a arrancar el modelo: {e}"})
        if not rec:
            return
        if not rec.get("gen"):
            self.push({"type": "error", "message": "Ninguna configuración arrancó: revisa que el modelo no esté dañado"})
            return
        self.push({"type": "info", "message": f"Mejor configuración: {rec['nombre']} — {rec['gen']} tokens/s generando, "
                   f"{rec['prompt']:.0f} tokens/s leyendo, contexto {rec['ctx'] // 1024}k ({rec['motivo']})."})

    def context(self, wanted: int) -> int:
        """La ventana que de verdad tiene el motor (si la publica); nunca más que esa."""
        own = self.server.proc and self.config["endpoint"].endswith(f":{self.server.port}/v1")
        real = models.server_context(self.config["endpoint"], self.server.api_key if own else "local")
        return min(wanted, real) if real else wanted

    def save_config(self) -> None:
        CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        CONFIG.write_text(json.dumps(self.config, indent=2))
        CONFIG.chmod(0o600)

    def push(self, ev: dict) -> None:
        with self.cond:
            ev["seq"] = self.base + len(self.log)
            self.log.append(ev)
            if len(self.log) > MAX_LOG:              # memoria acotada
                drop = len(self.log) - MAX_LOG
                del self.log[:drop]
                self.base += drop
            self.cond.notify_all()

    def pump(self, s: Session) -> None:
        while self.session is s:
            try:
                self.push(s.events.get(timeout=1))
            except Exception:  # noqa: BLE001 (queue.Empty)
                continue

    def events_since(self, seq: int, wait: float = 25) -> list[dict]:
        end = time.monotonic() + wait
        with self.cond:
            while self.base + len(self.log) <= seq and time.monotonic() < end:
                self.cond.wait(end - time.monotonic())
            return self.log[max(0, seq - self.base):]


APP: App


def git_branch(p: Path) -> str | None:
    """Rama actual si la carpeta es un repositorio git (sin ejecutar git)."""
    head = p / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
    except OSError:
        return None
    return ref.rsplit("/", 1)[-1] if ref.startswith("ref:") else ref[:8]


class Handler(BaseHTTPRequestHandler):
    server_version = "TismaCode/0.1"

    def log_message(self, *a):  # silencioso
        pass

    # ------------------------------------------------------------ utilidades
    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "_cookie", None):
            self.send_header("Set-Cookie", self._cookie)
            self._cookie = None
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass                     # el navegador cerró la conexión (p. ej. al recargar)

    def _host_ok(self) -> bool:
        # anti DNS-rebinding: el Host debe ser exactamente nuestra dirección
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _authorized(self) -> bool:
        return self._host_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), APP.token)

    def _cookie_ok(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "tc" and secrets.compare_digest(v, APP.token):
                return True
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(min(n, 5_000_000)) or b"{}")

    # ------------------------------------------------------------ GET
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if not self._host_ok():
            return self._send(403, {"error": "host no permitido"})
        if url.path == "/":
            boot = secrets.compare_digest(q.get("t", ""), APP.boot) and not APP.boot_used.is_set()
            if not (boot or self._cookie_ok()):
                return self._send(403, b"Abre TISMA Code desde su lanzador.", "text/plain")
            APP.boot_used.set()                       # la URL del argv ya no sirve a nadie más
            self._cookie = f"tc={APP.token}; HttpOnly; SameSite=Strict; Path=/"
            html = (STATIC / "index.html").read_text().replace("__TOKEN__", APP.token)
            return self._send(200, html.encode(), "text/html")
        if url.path in STATIC_FILES:                 # lista blanca: nada de rutas arbitrarias
            name, ctype = STATIC_FILES[url.path]
            return self._send(200, (STATIC / name).read_bytes(), ctype)
        if not self._authorized():
            return self._send(403, {"error": "no autorizado"})
        try:
            if url.path == "/api/state":
                s = APP.session
                return self._send(200, {"config": APP.config, "hardware": APP.hw,
                                        "workspace": str(s.ws.root) if s else None,
                                        "git": git_branch(s.ws.root) if s else None,
                                        "seq": APP.base + len(APP.log), "server_model": APP.server.model})
            if url.path == "/api/dirs":
                base = Path(q.get("path") or Path.home()).expanduser().resolve()
                dirs = sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
                return self._send(200, {"path": str(base), "parent": str(base.parent), "dirs": dirs[:500],
                                        "git": git_branch(base)})
            if url.path == "/api/events":
                return self._send(200, APP.events_since(int(q.get("since", 0))))
            if url.path == "/api/docs":
                if not APP.session:
                    return self._send(200, {"recent": [], "results": []})
                d = APP.session.docs
                return self._send(200, {"library": str(d.library), "project": str(d.project_dir),
                                        "recent": d.recent(), "results": d.search(q["q"]) if q.get("q") else []})
            if url.path == "/api/docs/read":
                if not APP.session:
                    return self._send(400, {"error": "Abre primero un proyecto"})
                return self._send(200, {"text": APP.session.docs.read(q["ref"])})
            if url.path == "/api/models/search":
                return self._send(200, models.search(q.get("q", "")))
            if url.path == "/api/models/files":
                return self._send(200, models.files(q.get("id", ""), APP.hw))
            if url.path == "/api/especialistas":
                from . import especialistas
                try:
                    specs, roles = especialistas.load()
                    err = ""
                except (especialistas.SpecialistError, ValueError) as e:
                    specs, roles, err = {}, {}, str(e)
                pool = APP.session.especialistas if APP.session else None
                return self._send(200, {"catalogo": especialistas.catalog(), "configurados": sorted(specs),
                                        "roles": roles, "error": err,
                                        "estadisticas": pool.stats if pool else {}})
            if url.path == "/api/models/local":
                return self._send(200, {"downloaded": models.local_models(),
                                        "endpoint": models.endpoint_models(APP.config["endpoint"]),
                                        "strata": models.strata_status(APP.hw), "motor": APP.motor_brief()})
            if url.path == "/api/motor":
                from . import motor
                return self._send(200, {"actual": APP.motor_brief(),
                                        "eleccion": motor.choose_engine(APP.hw, bool(models.endpoint_models(DEFAULT_ENDPOINT))),
                                        "recomendado": motor.choose_model(models.local_models(), APP.hw)})
        except (OSError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "no encontrado"})

    # ------------------------------------------------------------ POST
    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return self._send(403, {"error": "no autorizado"})
        url = urlparse(self.path)
        try:
            b = self._body()
            if url.path == "/api/session":
                old = APP.session
                if old:
                    old.cancel()
                APP.config.update({k: b[k] for k in ("endpoint", "model") if k in b})
                ws = b["workspace"]
                s = Session(ws, APP.config["endpoint"], APP.config["model"] or "local",
                            llm=APP.llm(), context_tokens=APP.context(int(b.get("context", 32768))))
                s.ws.allow_network = bool(b.get("network", False))
                APP.session = s
                if old:
                    old.close()                  # sin esto quedaban vivos llama-server y servidores MCP
                threading.Thread(target=APP.pump, args=(s,), daemon=True).start()
                APP.config["recent"] = [ws] + [r for r in APP.config["recent"] if r != ws][:7]
                APP.save_config()
                return self._send(200, {"workspace": str(s.ws.root)})
            if url.path == "/api/model":
                APP.config.update({k: b[k] for k in ("endpoint", "model") if b.get(k)})
                APP.save_config()
                if APP.session:
                    APP.session.endpoint, APP.session.model = APP.config["endpoint"], APP.config["model"]
                    APP.session.context_tokens = APP.context(APP.session.context_tokens)
                return self._send(200, {"ok": True})
            if url.path == "/api/message":
                if not APP.session:
                    return self._send(400, {"error": "Elige primero una carpeta de proyecto"})
                threading.Thread(target=APP.session.send, args=(b["text"],), daemon=True).start()
                return self._send(200, {"ok": True})
            if url.path == "/api/approve":
                ok = APP.session and APP.session.resolve_approval(b["id"], bool(b["allow"]), bool(b.get("always")))
                return self._send(200, {"ok": bool(ok)})
            if url.path == "/api/stop":
                if APP.session:
                    APP.session.cancel()
                return self._send(200, {"ok": True})
            if url.path in ("/api/council", "/api/hope/tool"):
                if not APP.session or not APP.session.hope:
                    return self._send(400, {"error": "Abre un proyecto (las herramientas de Hope necesitan una sesión)"})
                if url.path == "/api/hope/tool":
                    return self._send(200, {"result": APP.session.hope.run(b["nombre"], b["entrada"])})
                return self._send(200, APP.session.hope.council(b["problema"], b.get("tema") or "general"))
            if url.path == "/api/especialistas/instalar":
                from . import especialistas
                entry, gguf = especialistas.install(str(b["id"]))
                key = APP.downloads.start(entry["repo"], entry["archivo"], entry["sha256"])
                if APP.session:
                    APP.session.reload_especialistas()
                return self._send(200, {"key": key, "gguf": gguf})
            if url.path == "/api/models/download":
                return self._send(200, {"key": APP.downloads.start(b["id"], b["file"])})
            if url.path in ("/api/motor/optimizar", "/api/models/hilos"):
                path = str(Path(b["path"]).resolve())
                if models.MODELS_DIR.resolve() not in Path(path).parents:
                    return self._send(400, {"error": "Solo modelos descargados por TISMA Code"})
                threading.Thread(target=APP.optimize, args=(path,), daemon=True).start()
                return self._send(200, {"ok": True})
            if url.path == "/api/models/serve":
                ep = APP.server.start(b["path"], APP.hw)
                APP.config.update({"endpoint": ep, "model": Path(b["path"]).name})
                APP.save_config()
                if APP.session:
                    APP.session.endpoint, APP.session.model = ep, APP.config["model"]
                    APP.session.llm = APP.llm()
                    APP.session.context_tokens = APP.context(APP.server.ctx)
                return self._send(200, {"endpoint": ep})
        except (KeyError, ValueError, OSError, RuntimeError, ToolError) as e:  # SpecialistError es RuntimeError
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "no encontrado"})



def abrir(url: str, tamano, httpd) -> None:
    """Ventana propia de la app (sin pestañas ni barra de direcciones); al cerrarla, la app termina."""
    try:
        import sys
        sys.path.insert(0, "/usr/lib/centinela/python")
        from centinela.ventana import abrir_ventana
    except ImportError:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return
    abrir_ventana(url, "tisma-code", tamano,
                  al_cerrar=lambda: threading.Thread(target=httpd.shutdown, daemon=True).start())

def main(argv: list[str] | None = None) -> int:
    global APP
    ap = argparse.ArgumentParser(prog="tisma-code", description="Agente de programación con IA local")
    ap.add_argument("--port", type=int, default=0, help="0 = puerto libre aleatorio")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    APP = App()
    threading.Thread(target=APP.auto_motor, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{httpd.server_port}/?t={APP.boot}"
    print(f"TISMA Code en el puerto {httpd.server_port}", flush=True)     # la URL no va al registro
    if args.no_browser:
        print(url, flush=True)
    if not args.no_browser:
        abrir(url, None, httpd)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.server.stop()
    return 0


if __name__ == "__main__":
    os.sys.exit(main())
