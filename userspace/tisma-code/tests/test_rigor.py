"""El agente no debe dar la razón sin datos ni dar magnitudes sin calcularlas."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import harness  # noqa: E402
from tests.test_agent import call, run_session  # noqa: E402


def test_rigor_check_rules():
    assert harness.rigor_check("Tienes razón, el fallo es la caché.", set())
    assert harness.rigor_check("Sí, es eso.", {"update_todos"})
    assert harness.rigor_check("Tienes razón: lo confirmé.", {"read_file"}) is None
    assert harness.rigor_check("La corriente es 7,67 A y la caída 2,3 V.", set())
    assert harness.rigor_check("La corriente es 7,67 A y la caída 2,3 V.", {"hope"}) is None
    assert harness.rigor_check("No: los datos muestran otra causa.", set()) is None
    assert "hipótesis" in harness.hypothesis_note("El problema es la memoria, seguro que es eso")


def test_agent_is_forced_to_verify_instead_of_agreeing(tmp_path_factory):
    proj = tmp_path_factory.mktemp("p")
    (proj / "a.py").write_text("LIMITE = 10\n")
    script = [{"content": "¡Tienes razón! El límite es 100."},                       # complaciente, sin datos
              {"content": "", "tool_calls": [call("read_file", path="a.py")]},
              {"content": "No: el código fija LIMITE = 10, no 100.\nEvidencia: a.py:1."}]
    s, llm, events = run_session(proj, script, tmp_path_factory)
    assert s.stats["rigor_checks"] == 1
    assert any("[Control de rigor del arnés]" in (m.get("content") or "") for m in llm.seen)
    assert [e for e in events if e["type"] == "assistant"][-1]["text"].startswith("No: el código")


def test_grounding_check_catches_invented_references(tmp_path):
    from tisma_code import tools
    (tmp_path / "calc.py").write_text("def corriente(p, v):\n    return p / v\n")
    ws = tools.Workspace(str(tmp_path))
    ok = "La división está en calc.py:2 (`return p / v`), dentro de `corriente()`."
    assert harness.grounding_check(ok, ws) == []
    bad = "Ver calc.py:40 y `potencia()`; además `return p * v` en calc.py:2."
    issues = harness.grounding_check(bad, ws)
    assert any("calc.py:40" in i for i in issues)
    assert any("potencia()" in i for i in issues)
    assert any("return p * v" in i for i in issues)
    assert any("no existe" in i for i in harness.grounding_check("Está en src/fantasma.py:3", ws))


def test_untrusted_sources_are_labeled():
    assert harness.untrusted_wrap("jarvis", "dato").startswith("[Fuente NO verificada")
    assert harness.untrusted_wrap("read_file", "dato") == "dato"


def test_agent_corrects_hallucinated_citation(tmp_path_factory):
    proj = tmp_path_factory.mktemp("p")
    (proj / "a.py").write_text("LIMITE = 10\n")
    script = [{"content": "", "tool_calls": [call("read_file", path="a.py")]},
              {"content": "El límite se define en a.py:57.\nEvidencia: lectura."},       # línea inventada
              {"content": "", "tool_calls": [call("read_file", path="a.py")]},
              {"content": "El límite (`LIMITE = 10`) se define en a.py:1.\nEvidencia: a.py:1."}]
    s, llm, events = run_session(proj, script, tmp_path_factory)
    assert s.stats["rigor_checks"] == 1
    assert any("a.py:57" in (m.get("content") or "") for m in llm.seen)
