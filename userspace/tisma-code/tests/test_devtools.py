import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import devtools, tools  # noqa: E402
from tests.test_agent import call, run_session  # noqa: E402,F401

needs = lambda t: pytest.mark.skipif(not shutil.which(t), reason=f"sin {t}")  # noqa: E731


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "geo.py").write_text("class Punto:\n    def norma(self):\n        return 0\n\n"
                                     "def distancia(a, b):\n    return abs(a - b)\n")
    return tools.Workspace(str(tmp_path))


@needs("ctags")
def test_repo_map_and_symbol(ws):
    m = devtools.repo_map(ws)
    assert "geo.py" in m and "Punto" in m and "distancia" in m
    assert "geo.py:5" in devtools.find_symbol(ws, "distancia")


@needs("ruff")
def test_diagnostics_only_near_changes(ws):
    p = ws.root / "geo.py"
    before = p.read_text()
    p.write_text(before + "\ndef nueva():\n    return indefinida\n")
    import difflib
    d = "".join(difflib.unified_diff(before.splitlines(True), p.read_text().splitlines(True), "a/geo.py", "b/geo.py"))
    diags = devtools.diagnostics_near_changes(ws, "geo.py", d)
    assert diags and all(x["line"] >= 7 for x in diags) and any(x["code"] == "F821" for x in diags)


@needs("git")
def test_git_is_read_only(ws):
    for sub, args in (("push", []), ("diff", ["--output=/tmp/x"]), ("log", ["-c", "core.pager=sh"]),
                      ("branch", ["-D", "main"]), ("diff", ["--ext-diff"])):
        with pytest.raises(tools.ToolError):
            devtools.git(ws, sub, args)


@needs("rg")
def test_ripgrep_search(ws):
    assert "geo.py:5:" in devtools.rg_search(ws, r"def distancia")


@needs("ruff")
def test_agent_gets_lint_feedback(tmp_path_factory):
    proj = tmp_path_factory.mktemp("p")
    (proj / "a.py").write_text("def f():\n    return 1\n")
    script = [{"content": "", "tool_calls": [call("edit_file", path="a.py", old="return 1", new="return x")]},
              {"content": "Hecho."}]
    s, _, events = run_session(proj, script, tmp_path_factory)
    edit = next(e for e in events if e["type"] == "tool" and e["tool"] == "edit_file")
    assert "Linters en las líneas cambiadas" in edit["result"] and "F821" in edit["result"]
    assert s.stats["lint_feedback"] == 1
