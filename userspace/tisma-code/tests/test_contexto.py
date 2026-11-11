"""Gestión del contexto: cuaderno, salidas grandes fuera del contexto, reinicio limpio y exploración aparte."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import agent, contexto  # noqa: E402

RESUMEN = "## Hecho\nleí a.py\n## Hallazgos\nsuma en a.py:1\n## Decisiones\n—\n## Descartado\n—\n## Pendiente\narreglar"


def call(name, i=0, **args):
    return {"id": f"c{name}{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_cuaderno_persists_and_requires_evidence(tmp_path):
    c = contexto.Cuaderno(tmp_path)
    with pytest.raises(ValueError):
        c.anotar("hecho", "suma está mal")                       # sin evidencia no es un hecho
    with pytest.raises(ValueError):
        c.anotar("chisme", "x")
    c.anotar("hecho", "suma resta en vez de sumar", "read_file a.py:2")
    c.anotar("pendiente", "corregir suma")
    c.anotar("pendiente", "hecho: corregir suma")             # se tacha
    c.pedido("Arregla la suma")
    c2 = contexto.Cuaderno(tmp_path)                            # sobrevive (otro proceso, reinicio…)
    r = c2.render()
    assert "suma resta en vez de sumar [evidencia: read_file a.py:2]" in r and "corregir suma" not in r
    assert c2.data["pedidos"] == ["Arregla la suma"]


def test_saved_outputs_and_recordar(tmp_path):
    c = contexto.Cuaderno(tmp_path)
    n = c.guardar_salida("run_command", "\n".join(f"línea {i}" for i in range(1, 501)))
    assert "  300  línea 300" in c.recordar(salida=n, desde=300, lineas=2)
    c.archivar([{"role": "tool", "content": "El error está en parser.py:88 (índice fuera de rango)"}])
    assert "parser.py:88" in c.recordar("error parser")
    assert c.recordar(salida=999).startswith("No existe")


def test_resumir_map_reduce_and_fallback():
    msgs = [{"role": "tool", "content": "x" * 30000, "tool_call_id": str(i)} for i in range(20)]
    seen = []

    def chat(m):
        seen.append(m)
        return RESUMEN
    out = contexto.resumir(msgs, chat, context_tokens=8192)
    assert out.startswith("## Hecho") and len(seen) > 2              # varios tramos + fusión
    assert all(len(m[1]["content"]) < 8192 * 3.5 for m in seen)     # cada llamada cabe en la ventana
    bad = contexto.resumir([{"role": "assistant", "content": "voy",
                             "tool_calls": [call("read_file", path="a.py")]}], lambda m: "ok", 8192)
    assert "read_file a.py" in bad and contexto.valido(bad)          # resumen determinista


class LLM:
    def __init__(self, script, prompt_tokens=100):
        self.script, self.calls, self.pt = list(script), [], prompt_tokens

    def __call__(self, endpoint, model, messages, schemas, extra=None):
        self.calls.append((list(messages), schemas, extra))
        if schemas is None and "apartados" in (messages[0].get("content") or ""):
            return {"content": RESUMEN}, {}
        msg = self.script.pop(0) if self.script else {"content": "Listo."}
        return msg, {"prompt_tokens": self.pt, "completion_tokens": 10}


def session(tmp_path, llm, **kw):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.py").write_text("def suma(a, b):\n    return a - b\n" + "# relleno\n" * 3000)
    return agent.Session(str(proj), "http://x/v1", "m", llm=llm, kb_dir=tmp_path / "kb", auto_verify=False, **kw)


def test_restart_from_scratch_with_handoff(tmp_path):
    script = [{"content": "", "tool_calls": [call("anotar", 0, tipo="hecho", texto="suma resta",
                                                  evidencia="read_file a.py:2")]}]
    script += [{"content": "", "tool_calls": [call("read_file", i, path="a.py", limit=2000)]} for i in range(1, 4)]
    script += [{"content": "Hecho: suma corregida en a.py:2."}]
    llm = LLM(script, prompt_tokens=7000)            # el motor informa de 7000 tokens: > 60 % de 8192
    s = session(tmp_path, llm, context_tokens=8192)
    s.send("Arregla la función suma de a.py")
    assert s.stats["compactions"] >= 1
    first = s.messages[1]["content"]
    assert first.startswith("[Reinicio de contexto n.º")
    assert "Arregla la función suma de a.py" in first                  # pedido literal
    assert "suma resta [evidencia: read_file a.py:2]" in first          # cuaderno
    assert "## Hallazgos" in first and "suma en a.py:1" in first        # resumen
    summary_calls = [c for c in llm.calls if c[1] is None]
    assert summary_calls and summary_calls[0][2]["chat_template_kwargs"] == {"enable_thinking": False}
    assert list((tmp_path / "kb").rglob("historial-*.jsonl"))           # nada se pierde


def test_big_tool_output_goes_to_disk(tmp_path):
    llm = LLM([{"content": "", "tool_calls": [call("read_file", path="a.py", limit=3000)]}])
    s = session(tmp_path, llm, context_tokens=8192)
    s.send("Lee a.py")
    tool = next(m for m in s.messages if m["role"] == "tool")
    assert len(tool["content"]) < contexto.presupuesto_salida(8192) + 600
    n = int(tool["content"].split("guardada como n.º ")[1].split(":")[0])
    assert "relleno" in s.cuaderno.recordar(salida=n, desde=1500, lineas=3)


def test_explorar_isolated_context(tmp_path):
    script = [{"content": "", "tool_calls": [call("explorar", pregunta="¿Dónde está suma?")]},
              {"content": "", "tool_calls": [call("search", pattern="def suma")]},   # dentro del explorador
              {"content": "suma está en a.py:1"},                                     # conclusión
              {"content": "Está en a.py:1."}]
    llm = LLM(script)
    s = session(tmp_path, llm)
    s.send("¿Dónde está suma?")
    tool = next(m for m in s.messages if m["role"] == "tool")
    assert "suma está en a.py:1" in tool["content"]
    assert not any("def suma(a, b)" in (m.get("content") or "") for m in s.messages)   # la búsqueda no entró
    explorer_schemas = {t["function"]["name"] for t in llm.calls[1][1]}
    assert "write_file" not in explorer_schemas and "run_command" not in explorer_schemas
