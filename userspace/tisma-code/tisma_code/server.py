"""Servidor local de TISMA Code (solo 127.0.0.1, protegido con token)."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from typing import TYPE_CHECKING

from . import models
from .base import ToolError

if TYPE_CHECKING:
    from .agent import Session

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"/static/logo.svg": ("logo.svg", "image/svg+xml")}
MAX_LOG = 5000
CONFIG = Path.home() / ".config/tisma-code/config.json"
DEFAULT_ENDPOINT = "http://127.0.0.1:8080/v1"   # Strata


def enchufado(base: str = "/sys/class/power_supply") -> bool:
    """¿Corriente? Sin batería (equipo de escritorio) cuenta como enchufado."""
    try:
        fuentes = os.listdir(base)
    except OSError:
        return True
    hay_bateria = False
    for f in fuentes:
        try:
            tipo = open(os.path.join(base, f, "type")).read().strip()
            if tipo == "Mains" and open(os.path.join(base, f, "online")).read().strip() == "1":
                return True
            hay_bateria |= tipo == "Battery"
        except OSError:
            continue
    return not hay_bateria


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
        self.instalando: dict = {}
        self.server = models.LocalServer()
        self.hw = models.hardware()
        self.ultimo_uso = time.monotonic()
        self.config = {"endpoint": DEFAULT_ENDPOINT, "model": "", "recent": [], "network": False}
        try:
            self.config.update(json.loads(CONFIG.read_text()))
        except (OSError, ValueError):
            pass

    # Componentes que se instalan aparte (versión y SHA-256 fijados en centinela-apps): con un botón, sin terminal
    COMPONENTES = {"strata": ("Strata", "IA local para equipos con GPU dedicada y ≥32 GB (~300 MB)",
                              "/opt/strata/VERSION"),
                   "rea": ("REA", "análisis de binarios e ingeniería inversa (~200 MB)",
                           "/usr/lib/node_modules/rea-agents"),
                   "craft": ("Craft Anything", "CAD por código y recrear objetos (~35 MB)",
                             "/opt/craft-anything/VERSION"),
                   "solidcraft": ("SolidCraft", "CAD paramétrico 3D", "/var/lib/dpkg/info/solidcraft.list"),
                   "simulacion": ("Simulación", "OpenFOAM, FEniCSx, Netgen y ParaView (~1 GB)", "/usr/bin/paraview")}

    def _registro_comp(self, k: str) -> Path:
        d = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "tisma-code"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d / f"componente-{k}.log"

    def _ultima_linea(self, k: str) -> str:
        """Lo último que dijo la instalación (sin colores ni barras de progreso a medias), para mostrarlo."""
        try:
            texto = self._registro_comp(k).read_bytes()[-4000:].decode(errors="replace")
        except OSError:
            return ""
        lineas = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", x).strip() for x in re.split(r"[\r\n]+", texto)]
        lineas = [x for x in lineas if x and not re.fullmatch(r"[#=\-\s.%0-9]+", x)]
        return lineas[-1][:160] if lineas else ""

    def componentes(self) -> dict:
        out = {}
        for k, (nombre, desc, marca) in self.COMPONENTES.items():
            proc = self.instalando.get(k)
            estado = ("instalando" if proc and proc.poll() is None else
                      "instalado" if Path(marca).exists() else
                      "error" if proc and proc.returncode not in (0, None) else "no")
            c = {"nombre": nombre, "desc": desc, "estado": estado}
            if estado == "instalando":
                c["detalle"] = self._ultima_linea(k) or "Esperando la contraseña de administrador o empezando…"
            elif estado == "error":
                # pkexec: 126 = ventana de contraseña cerrada, 127 = sin permiso
                c["detalle"] = ("Se canceló: hace falta la contraseña de administrador." if proc.returncode in (126, 127)
                                else "No se pudo instalar: " + (self._ultima_linea(k) or "revisa la conexión a internet"))
            if k == "strata" and estado != "instalado" and not models.strata_status(self.hw).get("ok"):
                c["no_recomendado"] = "No hace falta en este equipo: llama.cpp ya es el motor adecuado."
            out[k] = c
        return out

    def ajustes(self) -> dict:
        """Lo que muestra «Ajustes»: descargas con su progreso, el motor, los componentes y la privacidad."""
        with self.downloads._lock:
            descargas = [dict(d) for d in self.downloads.state.values()]
        return {"descargas": descargas, "motor": self.motor_brief(), "componentes": self.componentes(),
                "trazas_hope": self.config.get("trazas_hope") is True,
                "auto_motor": self.config.get("auto_motor", True) is not False,
                "liberar_memoria": self.config.get("liberar_memoria", True) is not False,
                "pensar": self.config.get("pensar", "auto"),
                "esfuerzo": self.config.get("esfuerzo", "medio"),
                "pensar_efectivo": self.session.pensar_activo() if self.session else None}

    def pensar(self) -> bool | None:
        return {"si": True, "no": False}.get(self.config.get("pensar", "auto"))

    def instalar_componente(self, k: str) -> dict:
        if k not in self.COMPONENTES:
            return {"ok": False, "error": "componente desconocido"}
        proc = self.instalando.get(k)
        if proc and proc.poll() is None:
            return {"ok": True, "estado": "instalando"}
        # pkexec muestra la ventana de contraseña del sistema; la orden es fija (lista de arriba)
        log = open(self._registro_comp(k), "wb")      # el progreso se ve en Ajustes (y el motivo si falla)
        self.instalando[k] = subprocess.Popen(["pkexec", "/usr/bin/centinela-apps", k], stdout=log,
                                              stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        return {"ok": True, "estado": "instalando"}

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

    def usar_modelo(self, path: str) -> str:
        """Sirve este modelo (con lo medido para él, si se optimizó) y lo deja como el de siempre: al volver a
        abrir TISMA Code arranca este, no el que elegiría el motor."""
        ep = self.server.start(path, self.hw)
        # Las instrucciones ya procesadas de este modelo (guardadas en el disco): la primera respuesta no espera a
        # releerlas (~3000 tokens; en la A275, unos 20 s)
        threading.Thread(target=self.server.restaurar_prefijo, daemon=True).start()
        self.config.update({"endpoint": ep, "model": Path(path).name, "preferido": str(Path(path).resolve())})
        self.save_config()
        if self.session:
            self.session.endpoint, self.session.model = ep, self.config["model"]
            self.session.llm = self.llm()
            self.session.context_tokens = self.context(self.server.ctx)
        return ep

    def preferido(self) -> dict | None:
        """El modelo elegido (a mano, al optimizarlo o al reducirlo), si sigue descargado."""
        p = self.config.get("preferido")
        return next((m for m in models.local_models() if p and str(Path(m["path"]).resolve()) == p), None)

    def auto_motor(self) -> None:
        """Al abrir TISMA Code: si no hay motor en marcha, el motor TISMA elige y arranca el adecuado (salvo que se
        haya apagado en Ajustes)."""
        if self.config.get("auto_motor") is False:
            return
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
        rec = self.preferido() or motor.choose_model(models.local_models(), self.hw)
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
        threading.Thread(target=self.server.restaurar_prefijo, daemon=True).start()
        if eng.get("instalar"):
            self.push({"type": "info", "message": eng["motivo"]})
        c = self.server.config or {}
        self.push({"type": "info", "message": f"Motor TISMA: llama.cpp con {Path(rec['path']).name} — "
                   f"{c.get('motivo', '')}" + ("" if c.get("medido") else
                                                ". Cuando el equipo esté libre y enchufado se ajustará solo para ir "
                                                "más rápido.")})
        if not c.get("medido"):
            threading.Thread(target=self.optimizar_cuando_libre, args=(rec["path"],), daemon=True).start()

    LIBRE_S = 300
    REPOSO_S = 15 * 60

    def vigilar_reposo(self, espera=time.sleep, reloj=time.monotonic, una_vez: bool = False) -> None:
        """Tras 15 min sin usar TISMA Code, el motor se pausa y devuelve su memoria (varios GB con un modelo de
        7-8B) al resto del equipo. Antes guarda lo ya procesado: al volver a escribir arranca solo y responde casi
        igual de rápido (restaura el prefijo). La calidad no cambia: es el mismo modelo con la misma configuración."""
        while True:
            if not una_vez:
                espera(60)
            try:
                self.revisar_ecc()
            except Exception:  # noqa: BLE001 — nunca debe detener la vigilancia
                pass
            srv = self.server
            sesion = self.session
            ocupado = (sesion is not None and sesion._busy.locked()) or self._optimizing.locked() or \
                self._reduciendo.locked()
            if (self.config.get("liberar_memoria", True) is not False and srv.proc and srv.proc.poll() is None
                    and not ocupado and reloj() - self.ultimo_uso >= self.REPOSO_S):
                if not srv.prefijo_guardado:
                    srv.guardar_prefijo()
                self.dormido = getattr(srv, "path", None)
                srv.stop()
                self.push({"type": "info", "message": "Motor en pausa para liberar memoria (15 min sin uso). "
                           "Vuelve solo cuando escribas."})
            if una_vez:
                return

    dormido: str | None = None
    ECC_ALERTA = Path("/run/centinela/ecc-alerta.json")
    _ecc_visto = 0.0

    def revisar_ecc(self) -> bool:
        """El revisor de memoria (ECC por software, opcional) encontró datos dañados en el modelo que usa el motor
        y no pudo corregirlos en caliente: se vuelve a cargar el modelo desde el disco (en cuanto no esté
        respondiendo)."""
        try:
            a = json.loads(self.ECC_ALERTA.read_text())
        except (OSError, ValueError):
            return False
        srv = self.server
        if a.get("t", 0) <= self._ecc_visto or not srv.proc or srv.proc.poll() is not None:
            return False
        if os.path.realpath(a.get("archivo", "")) != os.path.realpath(getattr(srv, "path", "") or ""):
            return False
        if self.session is not None and self.session._busy.locked():
            return False                                   # se reintenta en el próximo minuto
        self._ecc_visto = a["t"]
        path = srv.path
        srv.stop()
        try:
            srv.start(path, self.hw)
        except (OSError, RuntimeError, ValueError) as e:
            self.push({"type": "error", "message": f"No se pudo recargar el modelo: {e}"})
            return False
        srv.restaurar_prefijo()
        self.push({"type": "info", "message": "La revisión de memoria encontró un error en el modelo cargado: se "
                   "volvió a cargar desde el disco. Si se repite, conviene pasar «Memory test» en el arranque."})
        return True

    def despertar(self) -> bool:
        """Si el motor está en pausa por reposo, lo vuelve a arrancar (mismo puerto y clave) antes de responder."""
        path, self.dormido = self.dormido, None
        if not path or (self.server.proc and self.server.proc.poll() is None):
            return False
        self.push({"type": "info", "message": "Despertando el motor…"})
        try:
            self.server.start(path, self.hw)
        except (OSError, RuntimeError, ValueError) as e:
            self.push({"type": "error", "message": f"No se pudo volver a arrancar el motor: {e}"})
            return False
        self.server.restaurar_prefijo()
        return True

    def optimizar_cuando_libre(self, path: str, espera=time.sleep, reloj=time.monotonic) -> bool:
        """Mide la configuración más rápida SOLA (antes había que encontrar «⚙ optimizar»): cuando TISMA Code lleva
        5 minutos sin usarse y el equipo está enchufado (medir carga la CPU y gasta batería). Una sola vez por
        modelo: lo medido queda guardado para este equipo."""
        while True:
            espera(60)
            sesion = self.session
            ocupado = sesion is not None and sesion._busy.locked()
            if reloj() - self.ultimo_uso >= self.LIBRE_S and not ocupado and enchufado():
                if self.server.model != Path(path).name:
                    return False                   # se cambió de modelo: ya no toca
                self.optimize(path)
                return True

    _reduciendo = threading.Lock()

    def reducir(self, path: str, nivel: str, capas: bool = False) -> None:
        """«✂ Reducir» en segundo plano, contando cada paso en el chat (puede tardar bastante en CPU)."""
        from . import reducir
        if not self._reduciendo.acquire(blocking=False):
            self.push({"type": "error", "message": "Ya se está reduciendo un modelo"})
            return
        try:
            self.push({"type": "info", "message": f"Reduciendo {Path(path).name}. Tarda un rato; puedes "
                       "seguir usando el equipo. El original no se toca."})
            r = reducir.reducir(path, nivel, capas, avisar=lambda m: None,
                                # el final lo marca el resumen con los tamaños (abajo), en la misma barra
                                progreso=lambda i, n, t: i < n and self.push({"type": "progreso", "id": "reducir",
                                                                              "titulo": "Reduciendo el modelo",
                                                                              "paso": i, "total": n, "texto": t}))
            capas = (f", sin {len(r['capas_quitadas'])} de {r['capas']} capas" if r["capas_quitadas"] else "")
            ahorro = round(100 * (1 - r["despues"] / r["antes"])) if r["antes"] else 0
            resumen = (f"{tamano_legible(r['antes'])} → {tamano_legible(r['despues'])} ({ahorro} % más chico, "
                       f"{r['tipo']}{capas}) · conserva un {r['calidad']:.0f} % de la calidad")
            self.push({"type": "progreso", "id": "reducir", "titulo": "Modelo reducido", "paso": 1, "total": 1,
                       "texto": resumen})                  # cierra la barra al 100 % con el tamaño final
            self.push({"type": "info", "message": f"Listo: {Path(r['archivo']).name} — {resumen}. "
                       "Queda como el modelo de siempre; ahora se busca su configuración más rápida."})
            self.optimize(r["archivo"])            # mide y guarda su mejor configuración, y lo deja en uso
        except Exception as e:  # noqa: BLE001 — se informa en el chat; el original queda intacto
            self.push({"type": "progreso", "id": "reducir", "titulo": "No se pudo reducir", "paso": 1, "total": 1,
                       "texto": "el original quedó intacto"})
            self.push({"type": "error", "message": f"No se pudo reducir: {e}"})
        finally:
            self._reduciendo.release()

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
            rec = motor.optimize(path, self.hw, on_step=lambda m: self.push({"type": "info", "message": "· " + m}),
                                 progreso=lambda i, n, t: self.push({"type": "progreso", "id": "optimizar",
                                                                     "titulo": "Optimizando el motor",
                                                                     "paso": i, "total": n, "texto": t}))
        except Exception as e:  # noqa: BLE001 (GGUF raro, motor que no arranca…): se avisa y se restaura
            self.push({"type": "progreso", "id": "optimizar", "titulo": "No se pudo optimizar", "paso": 1, "total": 1,
                       "texto": "se sigue con la configuración anterior"})
            self.push({"type": "error", "message": f"No se pudo optimizar: {e}"})
            rec = {}
        try:
            if rec.get("gen"):
                self.usar_modelo(path)    # optimizado: queda en uso y como el de siempre, con lo medido
            elif was_serving or not models.endpoint_models(self.config["endpoint"]):
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
                   f"{rec['prompt']:.0f} tokens/s leyendo, contexto {rec['ctx'] // 1024}k ({rec['motivo']}). "
                   "Queda guardada: este modelo arranca siempre así en este equipo."})

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
                ev = s.events.get(timeout=1)
            except Exception:  # noqa: BLE001 (queue.Empty)
                continue
            self.push(ev)
            # Al terminar la primera tarea de este arranque del motor (ya sin generar): guardar lo procesado
            if ev.get("type") in ("task_done", "idle") and self.server.proc and not self.server.prefijo_guardado:
                threading.Thread(target=self.server.guardar_prefijo, daemon=True).start()

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
            if url.path == "/api/componentes":
                return self._send(200, APP.componentes())
            if url.path == "/api/models/niveles":
                from . import reducir as red
                from .motor import gguf_info
                p = Path(q.get("path", "")).resolve()
                if models.MODELS_DIR.resolve() not in p.parents or not p.is_file():
                    return self._send(400, {"error": "Solo modelos descargados por TISMA Code"})
                origen = red.precision(p, gguf_info(str(p)).get("tipo_archivo"))
                return self._send(200, {"origen": origen, "niveles": red.niveles_posibles(origen),
                                        "manuales": [{"tipo": t, "bits": red.MANUALES[t][0], "desc": red.MANUALES[t][1]}
                                                     for t in red.manuales_posibles(origen)]})
            if url.path == "/api/ajustes":
                return self._send(200, APP.ajustes())
            if url.path == "/api/sin-apuro":
                from . import sin_apuro as sa
                modelos = []
                for m in models.local_models():
                    try:
                        e = sa.estimar(m["path"], APP.hw)
                    except (OSError, ValueError):
                        continue
                    modelos.append({"path": m["path"], "nombre": Path(m["path"]).name, "tamano": e["tamano"],
                                    "cabe": e["cabe"], "texto": sa.texto_estimacion(e)})
                return self._send(200, {"tareas": sa.listar(), "modelos": modelos})
            if url.path == "/api/sin-apuro/resultado":
                from . import sin_apuro as sa
                return self._send(200, {"texto": sa.resultado(q.get("id", ""))})
            if url.path == "/api/descargas":
                with APP.downloads._lock:
                    return self._send(200, {"descargas": [dict(d) for d in APP.downloads.state.values()]})
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
                from .agent import Session
                s = Session(ws, APP.config["endpoint"], APP.config["model"] or "local",
                            llm=APP.llm(), context_tokens=APP.context(int(b.get("context", 32768))))
                s.ws.allow_network = bool(b.get("network", False))
                if APP.config.get("trazas_hope") is True:      # opt-in: archivo local, nunca se envía
                    from .trazas import Trazas
                    s.trazas = Trazas(s.ws.root)
                s.pensar = APP.pensar()
                s.esfuerzo = APP.config.get("esfuerzo", "medio")
                APP.session = s
                if old:
                    old.close()                  # sin esto quedaban vivos llama-server y servidores MCP
                threading.Thread(target=APP.pump, args=(s,), daemon=True).start()
                APP.config["recent"] = [ws] + [r for r in APP.config["recent"] if r != ws][:7]
                APP.save_config()
                return self._send(200, {"workspace": str(s.ws.root)})
            if url.path == "/api/ajustes":
                # Solo estos ajustes, y solo con su tipo: la página no puede tocar el resto de la configuración
                for k in ("trazas_hope", "auto_motor", "liberar_memoria"):
                    if isinstance(b.get(k), bool):
                        APP.config[k] = b[k]
                if b.get("esfuerzo") in ("bajo", "medio", "alto", "maximo"):
                    APP.config["esfuerzo"] = b["esfuerzo"]
                    if APP.session:
                        APP.session.esfuerzo = b["esfuerzo"]
                if b.get("pensar") in ("auto", "si", "no"):
                    APP.config["pensar"] = b["pensar"]
                    if APP.session:
                        APP.session.pensar = APP.pensar()
                APP.save_config()
                return self._send(200, APP.ajustes())
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
                APP.ultimo_uso = time.monotonic()
                s, texto = APP.session, b["text"]

                def enviar():
                    APP.despertar()
                    s.send(texto)
                threading.Thread(target=enviar, daemon=True).start()
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
            if url.path == "/api/componentes/instalar":
                return self._send(200, APP.instalar_componente(str(b.get("nombre") or "")))
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
            if url.path == "/api/models/reducir":
                path = str(Path(b["path"]).resolve())
                if models.MODELS_DIR.resolve() not in Path(path).parents:
                    return self._send(400, {"error": "Solo modelos descargados por TISMA Code"})
                from . import reducir as red
                nivel = b.get("nivel") if b.get("nivel") in ("auto", *red.NIVELES, *red.MANUALES) else "auto"
                threading.Thread(target=APP.reducir, args=(path, nivel, b.get("capas") is True), daemon=True).start()
                return self._send(200, {"ok": True})
            if url.path == "/api/sin-apuro":
                from . import sin_apuro as sa
                adjunto, nombre = "", ""
                if b.get("archivo"):
                    if not APP.session:
                        return self._send(400, {"error": "Para adjuntar un archivo, abre primero su carpeta"})
                    f = (APP.session.ws.root / str(b["archivo"])).resolve()
                    if APP.session.ws.root.resolve() not in f.parents or not f.is_file():
                        return self._send(400, {"error": "El archivo tiene que estar dentro de la carpeta del proyecto"})
                    adjunto, nombre = f.read_text(errors="replace")[:sa.MAX_ADJUNTO], f.name
                t = sa.agregar(str(b.get("pedido") or ""), str(b.get("modelo") or ""), adjunto, nombre)
                sa.lanzar_trabajador()
                return self._send(200, {"id": t["id"]})
            if url.path in ("/api/sin-apuro/cancelar", "/api/sin-apuro/borrar"):
                from . import sin_apuro as sa
                f = sa.cancelar if url.path.endswith("cancelar") else sa.borrar
                return self._send(200, {"ok": f(str(b.get("id") or ""))})
            if url.path == "/api/models/serve":
                return self._send(200, {"endpoint": APP.usar_modelo(b["path"])})
        except (KeyError, ValueError, OSError, RuntimeError, ToolError) as e:  # SpecialistError es RuntimeError
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "no encontrado"})



def tamano_legible(n: int) -> str:
    """«612 MB» o «1,84 GB» (los modelos chicos pesan menos de 1 GB: «0,61 GB» se leía mal)."""
    if n < 1e9:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e9:.2f} GB".replace(".", ",")


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
    threading.Thread(target=APP.vigilar_reposo, daemon=True).start()
    # El agente (con Hope y numpy) tarda en cargar en equipos lentos: la ventana se abre primero y él se carga
    # mientras tanto, listo para cuando se elija la carpeta
    threading.Thread(target=lambda: __import__("tisma_code.agent"), daemon=True).start()
    from . import sin_apuro
    if sin_apuro.siguiente():              # quedaron tareas (p. ej. se apagó el equipo): se retoman
        sin_apuro.lanzar_trabajador()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    # Al cerrar la sesión (SIGTERM/SIGHUP) el motor quedaba vivo ocupando gigas de memoria: la señal ahora detiene
    # el servidor y pasa por el «finally» que apaga el motor (guardando su caché de n-gramas)
    import signal

    def _salir(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _salir)
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
        if APP.session:
            APP.session.close()          # sus servidores MCP y especialistas
        n = models.cerrar_motores_hijos()  # y cualquier motor que haya quedado (nunca queda uno ocupando memoria)
        print(f"TISMA Code cerrado ({n} motor(es) apagado(s))", flush=True)
    return 0


if __name__ == "__main__":
    os.sys.exit(main())
