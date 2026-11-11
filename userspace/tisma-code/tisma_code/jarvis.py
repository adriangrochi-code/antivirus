"""OpenJarvis (Stanford, Apache-2.0) como asistente de investigación para TISMA Code.

OpenJarvis corre como servicio de usuario en 127.0.0.1:8000 con su propio agente, memoria y
búsqueda web, usando el mismo modelo local (Strata). TISMA Code le delega preguntas de
investigación («¿qué librería…?», «resume este estándar…»): el resultado vuelve como texto.

Seguridad: la configuración de TISMA (/usr/share/tisma/openjarvis/config.toml) desactiva shell,
escritura de archivos y analítica, limita los directorios a la biblioteca de documentos y exige
clave. Como puede buscar en Internet, cada consulta pide permiso al usuario.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = os.environ.get("TISMA_JARVIS_URL", "http://127.0.0.1:8000")
KEY_FILE = Path.home() / ".config/tisma-code/openjarvis.env"


def _key() -> str:
    try:
        for line in KEY_FILE.read_text().splitlines():
            if line.startswith("OPENJARVIS_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def ask(question: str, timeout: int = 600) -> str:
    from .agent import _check_local_owner, LLMError       # mismo control anti-suplantación
    try:
        _check_local_owner(ENDPOINT + "/v1")
    except LLMError:
        return ("OpenJarvis no está en marcha (o el puerto no es tuyo). Actívalo con: "
                "systemctl --user start openjarvis")
    body = {"model": "default", "messages": [{"role": "user", "content": question}],
            "max_tokens": 2048, "temperature": 0.3}
    req = urllib.request.Request(ENDPOINT + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {_key()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (localhost)
            data = json.load(r)
        return data["choices"][0]["message"].get("content") or "(sin respuesta)"
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        return f"Error al consultar OpenJarvis: {e}"


SCHEMAS = [{"type": "function", "function": {
    "name": "jarvis", "description": "Pregunta de investigación a OpenJarvis (asistente local con memoria y "
    "búsqueda web): documentación, comparar librerías, estándares, resúmenes. Pide permiso porque puede usar "
    "Internet. No le envíes código privado ni secretos.",
    "parameters": {"type": "object", "required": ["pregunta"], "properties": {"pregunta": {"type": "string"}}}}}]
