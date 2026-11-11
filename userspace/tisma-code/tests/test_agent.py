"""Pruebas del arnés de TISMA Code con un modelo simulado (sin GPU ni red)."""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import agent, harness, review, tools  # noqa: E402


def call(name, **args):
    return {"id": f"c{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeLLM:
    """Devuelve una secuencia fija de mensajes del «modelo»."""
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def __call__(self, endpoint, model, messages, schemas):
        self.seen.append(messages[-1])
        msg = self.script.pop(0) if self.script else {"content": "Listo."}
        return msg, {"prompt_tokens": 100, "completion_tokens": 20}


@pytest.fixture
def project(tmp_path):
    (tmp_path / "calc.py").write_text("def suma(a, b):\n    return a - b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from calc import suma\n\ndef test_suma():\n    assert suma(2, 3) == 5\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    return tmp_path


def run_session(project, script, tmp_path_factory, **kw):
    llm = FakeLLM(script)
    s = agent.Session(str(project), "http://x/v1", "m", llm=llm, kb_dir=tmp_path_factory.mktemp("kb"), **kw)
    s.always_allow |= {"write_file", "edit_file", "run_command"}
    s.send("Arregla la función suma")
    events = []
    while not s.events.empty():
        events.append(s.events.get())
    return s, llm, events


@pytest.mark.skipif(not shutil.which("pytest") and not shutil.which("python3"), reason="sin python")
def test_review_loop_fuzzy_edit_and_learning(project, tmp_path_factory):
    script = [
        {"content": "", "tool_calls": [call("read_file", path="calc.py")]},
        # sangría incorrecta: el arnés la tolera
        {"content": "", "tool_calls": [call("edit_file", path="calc.py", old="\treturn a - b", new="\treturn a * b")]},
        {"content": "Cambié la resta."},                                  # termina → verify falla
        {"content": "", "tool_calls": [call("edit_file", path="calc.py", old="  return a * b", new="  return a + b")]},
        {"content": "Corregido: ahora suma."},                            # verify pasa
    ]
    s, llm, events = run_session(project, script, tmp_path_factory)
    assert (project / "calc.py").read_text() == "def suma(a, b):\n    return a + b\n"
    verifies = [e for e in events if e["type"] == "verify"]
    assert [v["ok"] for v in verifies] == [False, True]
    assert any("Verificación automática" in (m.get("content") or "") for m in llm.seen)
    assert s.stats["fuzzy_edits"] >= 1 and s.stats["verified_tasks"] == 1
    assert (s.kb.dir / "episodios.jsonl").exists()
    ctx, ids = s.kb.context("Arregla la función suma")
    assert ids and "LECCIÓN" in ctx


def test_text_tool_calls_are_parsed(project, tmp_path_factory):
    script = [{"content": 'Voy a mirar.\n<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>'},
              {"content": "Hay calc.py."}]
    s, _, events = run_session(project, script, tmp_path_factory)
    assert s.stats["repaired_calls"] == 1
    assert any(e["type"] == "tool" and e["tool"] == "list_dir" and e["ok"] for e in events)


def test_workspace_confinement(project):
    ws = tools.Workspace(str(project))
    with pytest.raises(tools.ToolError):
        ws.resolve("../../etc/passwd")
    with pytest.raises(tools.ToolError):
        ws.resolve("/etc/passwd")


def test_repair_json_and_loop_detector():
    assert harness.repair_json('```json\n{"a": 1,}\n```') == {"a": 1}
    det = harness.LoopDetector()
    assert [det.check("read_file", {"path": "x"}) for _ in range(3)] == [False, False, True]


def test_router_escalation_and_review_states():
    r = review.Router([review.ModelSpec("veloz", "e", "a", 0), review.ModelSpec("potente", "e", "b", 1)])
    assert r.first().name == "veloz" and r.escalate(r.first()).name == "potente"
    loop = review.ReviewLoop(max_rounds=2)
    fail = "$ pytest\n[código de salida 1]\nFAILED test_x"
    assert loop.next(fail) == "reintentar"
    assert loop.next(fail) == "sin_progreso"
    assert review.ReviewLoop().next("$ pytest\n[código de salida 0]\n1 passed") == "verificado"


def test_hope_critic_on_calculations(project, tmp_path_factory):
    pytest.importorskip("numpy")
    script = [{"content": "Paso 1: 6 * 7 = 41\nResultado: 41"},
              {"content": "", "tool_calls": [call("hope", nombre="calc", entrada="6 * 7")]},
              {"content": "Paso 1: 6 * 7 = 42\nResultado: 42"}]
    s, llm, events = run_session(project, script, tmp_path_factory)
    if s.hope is None:
        pytest.skip("Hope no disponible")
    verifies = [e for e in events if e["type"] == "verify"]
    assert verifies[0]["ok"] is False and verifies[-1]["ok"] is True
    assert any(e["type"] == "tool" and e["tool"] == "hope" and "42" in e["result"] for e in events)


def test_docs_tools_in_agent(project, tmp_path_factory, monkeypatch):
    lib = tmp_path_factory.mktemp("lib")
    (lib / "manual.md").write_text("# Manual\n\nEl factor de potencia del motor es 0,85.\n")
    monkeypatch.setattr("tisma_code.docs.LIBRARY", lib)
    import tisma_code.agent as ag
    orig = ag.DocSpace
    monkeypatch.setattr(ag, "DocSpace", lambda root, idx: orig(root, idx, library=lib))
    script = [{"content": "", "tool_calls": [call("docs_buscar", consulta="factor de potencia")]},
              {"content": "", "tool_calls": [call("docs_guardar", tipo="descubrimiento", titulo="FP del motor",
                                                  contenido="Es 0,85 según el manual.")]},
              {"content": "Anotado."}]
    s, _, events = run_session(project, script, tmp_path_factory)
    tools_ev = [e for e in events if e["type"] == "tool"]
    assert tools_ev[0]["ok"] and "manual.md" in tools_ev[0]["result"]
    saved = [e for e in events if e["type"] == "doc_saved"]
    assert saved and saved[0]["doc_kind"] == "descubrimiento"
    assert not [e for e in events if e["type"] == "error"]
    assert list((project / ".tisma" / "descubrimientos").glob("*.md"))
