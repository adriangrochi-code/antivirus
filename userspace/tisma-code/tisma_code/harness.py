"""Arnés: técnicas para sacar más calidad y eficiencia de modelos locales.

Los modelos locales (7B-120B) fallan más que los grandes en tres cosas:
formato de las llamadas a herramientas, ediciones exactas y mantener el
rumbo en tareas largas. Este módulo compensa cada una sin depender del modelo.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# ---------------------------------------------------------------- llamadas en texto
_TOOL_TAG = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
_FENCE = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", re.S)


def repair_json(text: str):
    """Intenta interpretar JSON imperfecto típico de modelos pequeños."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    for candidate in (t, re.sub(r",\s*([}\]])", r"\1", t)):
        try:
            return json.loads(candidate)
        except ValueError:
            pass
    # Primer objeto {...} balanceado
    start = t.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(t[start:], start):
            if in_str:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(re.sub(r",\s*([}\]])", r"\1", t[start:i + 1]))
                    except ValueError:
                        break
    raise ValueError("JSON irreparable")


def _as_call(obj, n: int) -> dict | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("function")
    args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
    if isinstance(name, dict):  # {"function": {"name":..., "arguments":...}}
        args = name.get("arguments", args)
        name = name.get("name")
    if not isinstance(name, str):
        return None
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    return {"id": f"txt{n}", "type": "function", "function": {"name": name, "arguments": args}}


def extract_text_tool_calls(content: str, known: set[str]) -> list[dict]:
    """Llamadas escritas como texto (<tool_call>…</tool_call> o bloques ```json)."""
    calls = []
    for blob in _TOOL_TAG.findall(content) or _FENCE.findall(content):
        try:
            c = _as_call(repair_json(blob), len(calls))
        except ValueError:
            continue
        if c and c["function"]["name"] in known:
            calls.append(c)
    return calls


def normalize_arguments(raw) -> tuple[dict | None, str | None]:
    """Devuelve (args, error). El error se devuelve al modelo para que se corrija."""
    if isinstance(raw, dict):
        return raw, None
    try:
        obj = repair_json(raw or "{}")
    except ValueError:
        return None, ("Los argumentos no son JSON válido. Reintenta con un objeto JSON, "
                      'por ejemplo {"path": "src/main.py"}.')
    if not isinstance(obj, dict):
        return None, "Los argumentos deben ser un objeto JSON."
    return obj, None


# ---------------------------------------------------------------- edición tolerante
def fuzzy_replace(text: str, old: str, new: str) -> str | None:
    """Reemplazo tolerante a espacios/sangría. Solo si la coincidencia es única."""
    lines = text.splitlines(keepends=True)
    old_lines = [line.strip() for line in old.strip("\n").splitlines()]
    if not old_lines or not any(old_lines):
        return None
    n = len(old_lines)
    matches = [i for i in range(len(lines) - n + 1)
               if [line.strip() for line in lines[i:i + n]] == old_lines]
    if len(matches) != 1:
        return None
    i = matches[0]
    real_indent = re.match(r"[ \t]*", lines[i]).group(0)
    new_lines = new.strip("\n").splitlines()
    model_indent = re.match(r"[ \t]*", new_lines[0]).group(0) if new_lines else ""
    fixed = []
    for line in new_lines:
        if line.startswith(model_indent):
            line = real_indent + line[len(model_indent):]
        fixed.append(line + "\n")
    if fixed and not lines[i + n - 1].endswith("\n"):
        fixed[-1] = fixed[-1].rstrip("\n")
    return "".join(lines[:i] + fixed + lines[i + n:])


# ---------------------------------------------------------------- verificación
def detect_checks(root: Path) -> list[str]:
    """Comandos de verificación del proyecto, en orden de preferencia."""
    checks = []
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or list(root.glob("tests/test_*.py")):
        checks.append("PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -x -p no:cacheprovider 2>&1 | tail -n 40")
    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
            if "test" in scripts:
                checks.append("npm test --silent 2>&1 | tail -n 40")
            elif "build" in scripts:
                checks.append("npm run build --silent 2>&1 | tail -n 40")
        except ValueError:
            pass
    if (root / "Cargo.toml").exists():
        checks.append("cargo test --offline -q 2>&1 | tail -n 40")
    if (root / "go.mod").exists():
        checks.append("go test ./... 2>&1 | tail -n 40")
    mk = root / "Makefile"
    if mk.exists() and re.search(r"^test:", mk.read_text(errors="ignore"), re.M):
        checks.append("make test 2>&1 | tail -n 40")
    if (root / "CMakeLists.txt").exists() and (root / "build").is_dir():
        checks.append("cmake --build build 2>&1 | tail -n 30 && ctest --test-dir build --output-on-failure 2>&1 | tail -n 30")
    return checks


def project_memory(root: Path, limit: int = 6000) -> str:
    for name in ("TISMA.md", "AGENTS.md", "CLAUDE.md"):
        p = root / name
        if p.is_file() and not p.is_symlink():
            return f"\n\nInstrucciones del proyecto ({name}):\n" + p.read_text(errors="replace")[:limit]
    return ""


# ---------------------------------------------------------------- contexto
def estimate_tokens(messages: list[dict]) -> int:
    chars = sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls", ""))) for m in messages)
    return int(chars / 3.5)


def compaction_prompt(messages: list[dict]) -> list[dict]:
    transcript = []
    for m in messages:
        role = m["role"]
        content = (m.get("content") or "")[:3000]
        if m.get("tool_calls"):
            content += " " + ", ".join(c["function"]["name"] for c in m["tool_calls"])
        transcript.append(f"[{role}] {content}")
    return [{"role": "system", "content": "Resume esta sesión de programación para continuarla. Incluye: "
             "objetivo del usuario, archivos tocados y su estado, decisiones tomadas, errores pendientes "
             "y próximos pasos. Máximo 400 palabras."},
            {"role": "user", "content": "\n".join(transcript)}]


class LoopDetector:
    """Detecta cuando el modelo repite la misma llamada (bucle típico de modelos pequeños)."""

    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self.history: list[str] = []

    def check(self, name: str, args: dict) -> bool:
        sig = hashlib.sha1(f"{name}{json.dumps(args, sort_keys=True)}".encode()).hexdigest()
        self.history.append(sig)
        return self.history[-self.threshold:].count(sig) >= self.threshold


TODO_SCHEMA = {"type": "function", "function": {
    "name": "update_todos",
    "description": "Mantén una lista de tareas para trabajos de varios pasos. Envía la lista completa "
                   "cada vez, marcando estado pendiente / en_curso / hecha.",
    "parameters": {"type": "object", "required": ["todos"], "properties": {"todos": {
        "type": "array", "items": {"type": "object", "required": ["task", "status"], "properties": {
            "task": {"type": "string"},
            "status": {"type": "string", "enum": ["pendiente", "en_curso", "hecha"]}}}}}}}}

VERIFY_SCHEMA = {"type": "function", "function": {
    "name": "verify", "description": "Ejecuta las comprobaciones del proyecto (tests/compilación) "
    "detectadas automáticamente. Úsalo después de cambiar código.",
    "parameters": {"type": "object", "properties": {}}}}
