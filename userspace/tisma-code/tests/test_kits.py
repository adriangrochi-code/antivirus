"""Kits de herramientas: el modelo pequeño ve solo lo que la tarea necesita."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import agent, kits  # noqa: E402


def call(name, i=0, **args):
    return {"id": f"c{name}{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class LLM:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, endpoint, model, messages, schemas, extra=None):
        self.calls.append({t["function"]["name"] for t in schemas or []})
        return (self.script.pop(0) if self.script else {"content": "Listo."}), {}


def session(tmp_path, llm, ctx):
    (tmp_path / "p").mkdir()
    return agent.Session(str(tmp_path / "p"), "http://x/v1", "m", llm=llm, kb_dir=tmp_path / "kb",
                         auto_verify=False, context_tokens=ctx)


def test_small_model_sees_core_plus_matching_kits(tmp_path):
    llm = LLM([{"content": "", "tool_calls": [call("herramientas", kit="cad")]},
               {"content": "", "tool_calls": [call("docs_buscar", consulta="viga")]},   # kit inactivo: se ejecuta
               {"content": "Hecho."}])
    s = session(tmp_path, llm, 16384)
    s.send("Calcula la flecha de una viga de 1 m con 500 N")
    first, second, third = llm.calls[:3]
    assert kits.NUCLEO <= first and {"hope", "ingenieria"} <= first
    assert "rea" not in first and "craft" not in first and "solidcraft" not in first
    assert "solidcraft" in second and "docs_buscar" in third
    tool_msgs = [m["content"] for m in s.messages if m["role"] == "tool"]
    assert "Kit activado" in tool_msgs[0] and "solidcraft_guia" in tool_msgs[0]
    assert "kit desconocido" not in tool_msgs[1]


def test_schemas_much_smaller_for_small_models():
    full = [*__import__("tisma_code.tools", fromlist=["x"]).TOOL_SCHEMAS, kits.SCHEMA]
    small = kits.visible(full, set(), 16384)
    assert len(json.dumps(small)) < len(json.dumps(full)) / 2.5
    assert kits.visible(full, set(), 131072) == full                 # modelo grande: todas


def test_kits_reset_per_task(tmp_path):
    llm = LLM([])
    s = session(tmp_path, llm, 16384)
    s.send("Simula el circuito filtro.cir con ngspice")
    s.send("Lista los archivos")
    assert "ingenieria" in llm.calls[0] and "ingenieria" not in llm.calls[1]
