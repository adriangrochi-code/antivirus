"""Pasarela de IA para estudiantes (TISMA como proveedor). API compatible con OpenAI para que TISMA Asistente y
TISMA Code la usen igual que un motor local.

  POST /v1/vincular              {"codigo", "alias"}        → {"ficha"} (el equipo del estudiante se vincula)
  GET  /v1/models                                           → el modelo escolar
  POST /v1/chat/completions      Bearer <ficha estudiante>  → respuesta del tutor (sin streaming en esta versión)
  GET  /docente/resumen          Bearer <ficha docente>     → temas y estudiantes activos de SUS grupos
  GET  /docente/alertas          Bearer <ficha docente>     → alertas de riesgo de SUS grupos (categoría y resumen)
  POST /docente/alertas/<id>/atendida

Va detrás de un proxy con TLS (Caddy o nginx): escucha solo en 127.0.0.1. El modelo es abierto y propio (Qwen u
otro, y Hope cuando esté listo), servido con una API OpenAI local (vLLM o llama.cpp): los datos de menores no salen
de los servidores de TISMA. El texto de las conversaciones no se guarda nunca (ver datos.py).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import politica
from .datos import Datos

MAX_CUERPO = 64 << 10           # una pregunta escolar no necesita más
MAX_MENSAJES = 40
MODELO = "tisma-escolar"


def llamar_modelo(endpoint: str, modelo: str, mensajes: list[dict], max_tokens: int, timeout: int = 120) -> tuple[str, int]:
    body = json.dumps({"model": modelo, "messages": mensajes, "max_tokens": max_tokens, "temperature": 0.4}).encode()
    req = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (motor propio en la red interna)
        d = json.load(r)
    texto = d["choices"][0]["message"].get("content") or ""
    return texto, int((d.get("usage") or {}).get("total_tokens") or 0)


def limpiar_mensajes(mensajes) -> list[dict] | None:
    """Solo turnos de usuario y asistente, texto plano y acotado: el sistema lo pone SIEMPRE el servidor."""
    if not isinstance(mensajes, list) or not mensajes:
        return None
    out = []
    for m in mensajes[-MAX_MENSAJES:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        c = m.get("content")
        if isinstance(c, list):                          # formato por partes: solo el texto
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        if not isinstance(c, str) or not c.strip():
            continue
        out.append({"role": m["role"], "content": c[:6000]})
    return out if out and out[-1]["role"] == "user" else None


class App:
    def __init__(self, datos: Datos, endpoint: str, modelo: str, ayuda: list[str] | None = None, llamar=None):
        self.datos, self.endpoint, self.modelo = datos, endpoint, modelo
        self.ayuda = ayuda or politica.AYUDA_UY
        self.llamar = llamar or llamar_modelo

    def responder(self, cuenta: dict, mensajes: list[dict]) -> tuple[int, dict]:
        if self.datos.cupo_restante(cuenta) <= 0:
            return 429, {"error": {"message": "Llegaste al límite de preguntas de hoy. ¡Mañana seguimos! Mientras "
                                              "tanto, probá resolverlo con tus apuntes o consultá a tu docente."}}
        pregunta = mensajes[-1]["content"]
        a = politica.analizar(pregunta)
        nivel = cuenta["nivel"]
        if a.bloqueo:
            texto, tokens = politica.REDIRECCION[a.bloqueo], 0
        else:
            sistema = politica.sistema(nivel, self.ayuda)
            if a.riesgo:
                sistema += ("\nIMPORTANTE: en este mensaje hay señales de que el estudiante puede estar en riesgo "
                            f"({a.riesgo}). Respondé con calma, validá lo que siente, no juzgues, no des detalles "
                            "de métodos y animalo a hablar YA con un adulto de confianza.")
            if a.datos_personales:
                sistema += "\nEl estudiante compartió un dato personal: recordale con amabilidad que no hace falta."
            try:
                texto, tokens = self.llamar(self.endpoint, self.modelo,
                                            [{"role": "system", "content": sistema}, *mensajes],
                                            politica.NIVELES[nivel]["max_tokens"])
            except (urllib.error.URLError, OSError, KeyError, ValueError):
                if not a.riesgo:
                    return 503, {"error": {"message": "La IA no está disponible en este momento. Probá de nuevo en "
                                                      "un rato."}}
                texto, tokens = "Gracias por contármelo. Lo que te pasa importa.", 0
            texto, _ = politica.revisar_salida(texto, nivel)
            if a.riesgo:
                texto += politica.pie_de_ayuda(self.ayuda)   # las líneas de ayuda van SIEMPRE, diga lo que diga el modelo
        self.datos.registrar(cuenta, a.tema, tokens, a.riesgo)
        return 200, {"id": f"chatcmpl-{int(time.time() * 1000)}", "object": "chat.completion", "model": MODELO,
                     "choices": [{"index": 0, "message": {"role": "assistant", "content": texto},
                                  "finish_reason": "stop"}],
                     "usage": {"total_tokens": tokens}}


def handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "tisma-ia-escolar"
        sys_version = ""

        def log_message(self, *a):                       # sin registros con rutas ni direcciones de menores
            pass

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _cuenta(self, tipo: str) -> dict | None:
            m = re.fullmatch(r"Bearer (\S{20,200})", self.headers.get("Authorization", ""))
            c = app.datos.cuenta(m.group(1)) if m else None
            return c if c and c["tipo"] == tipo else None

        def _cuerpo(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= MAX_CUERPO:
                return None
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return None

        def do_GET(self):
            if self.path == "/v1/models":
                if not self._cuenta("estudiante"):
                    return self._send(401, {"error": {"message": "sin autorización"}})
                return self._send(200, {"object": "list", "data": [{"id": MODELO, "object": "model"}]})
            if self.path in ("/docente/resumen", "/docente/alertas"):
                d = self._cuenta("docente")
                if not d:
                    return self._send(401, {"error": "sin autorización"})
                if self.path.endswith("resumen"):
                    return self._send(200, app.datos.resumen(d["id"]))
                return self._send(200, {"alertas": app.datos.alertas(d["id"])})
            return self._send(404, {"error": "no existe"})

        def do_POST(self):
            if self.path == "/v1/vincular":
                b = self._cuerpo()
                if not isinstance(b, dict) or not re.fullmatch(r"[A-Za-z0-9]{8}", str(b.get("codigo", ""))):
                    return self._send(400, {"error": "código inválido"})
                r = app.datos.vincular(str(b["codigo"]), str(b.get("alias") or ""))
                if not r:
                    return self._send(403, {"error": "El código no es válido o venció: pedile uno nuevo a tu docente"})
                return self._send(200, {"ficha": r[1], "modelo": MODELO})
            if self.path == "/v1/chat/completions":
                c = self._cuenta("estudiante")
                if not c:
                    return self._send(401, {"error": {"message": "sin autorización"}})
                b = self._cuerpo()
                msgs = limpiar_mensajes(b.get("messages") if isinstance(b, dict) else None)
                if msgs is None:
                    return self._send(400, {"error": {"message": "pedido inválido"}})
                if isinstance(b, dict) and b.get("stream"):
                    return self._send(400, {"error": {"message": "esta versión no admite streaming"}})
                return self._send(*app.responder(c, msgs))
            m = re.fullmatch(r"/docente/alertas/(\d{1,12})/atendida", self.path)
            if m:
                d = self._cuenta("docente")
                if not d:
                    return self._send(401, {"error": "sin autorización"})
                ok = app.datos.marcar_atendida(d["id"], int(m.group(1)))
                return self._send(200 if ok else 404, {"ok": ok})
            return self._send(404, {"error": "no existe"})

    return H


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tisma-ia-escolar")
    ap.add_argument("--db", default="/var/lib/tisma-ia-escolar/datos.db")
    ap.add_argument("--puerto", type=int, default=8780)
    ap.add_argument("--motor", default="http://127.0.0.1:8000/v1", help="API OpenAI del modelo abierto (vLLM/llama.cpp)")
    ap.add_argument("--modelo", default="qwen")
    a = ap.parse_args(argv)
    pimienta = Path(os.environ.get("TISMA_IA_PIMIENTA_ARCHIVO", "/etc/tisma-ia-escolar/pimienta")).read_bytes()
    datos = Datos(a.db, pimienta)
    datos.purgar()
    srv = ThreadingHTTPServer(("127.0.0.1", a.puerto), handler(App(datos, a.motor, a.modelo)))
    srv.serve_forever()
    return 0
