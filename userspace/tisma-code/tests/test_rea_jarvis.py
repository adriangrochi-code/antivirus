"""REA vía MCP con un servidor MCP falso (sin Node) y filtros de seguridad."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import rea, tools  # noqa: E402
from tisma_code.mcp_client import MCPError  # noqa: E402

FAKE = r'''
import json, sys
TOOLS = [
 {"name": "binary_overview", "description": "Overview of a native binary", "annotations": {"readOnlyHint": True},
  "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
 {"name": "start_frida_session", "description": "Attach Frida", "annotations": {"openWorldHint": False}},
 {"name": "inspect_web_page", "description": "Open a page in Chrome", "annotations": {"openWorldHint": True}},
 {"name": "annotate_native_function", "description": "Rename function", "annotations": {"readOnlyHint": False}},
 {"name": "sneaky", "description": "x", "_meta": {"effects": {"executesForeignCode": True}}},
 {"name": "rea_self_update", "description": "update REA"},
 {"name": "wipe_target", "description": "x", "annotations": {"destructiveHint": True}},
]
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    if m["method"] == "initialize": r = {"protocolVersion": "2025-06-18", "capabilities": {}}
    elif m["method"] == "tools/list": r = {"tools": TOOLS}
    elif m["method"] == "tools/call":
        r = {"content": [{"type": "text", "text": "ELF 64-bit, PIE, NX: " + json.dumps(m["params"]["arguments"])}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


def make(tmp_path, **kw):
    srv = tmp_path / "fake_mcp.py"
    srv.write_text(FAKE)
    ws = tools.Workspace(str(tmp_path))
    return rea.REATools(ws, command=[sys.executable, str(srv)], jail=False, **kw)


def test_rea_dynamic_mode_allows_runtime_with_approval(tmp_path):
    r = make(tmp_path, dynamic=True)
    try:
        assert r.kind["start_frida_session"] == "aprobar" and r.kind["sneaky"] == "aprobar"
        assert r.kind["inspect_web_page"] == "bloqueada"           # usa red: permiso aparte
        assert r.kind["rea_self_update"] == "bloqueada" and r.kind["wipe_target"] == "bloqueada"
    finally:
        r.close()
    r = make(tmp_path, dynamic=True, network=True)
    try:
        assert r.kind["inspect_web_page"] == "aprobar"
    finally:
        r.close()


def test_rea_guides_progressive_disclosure():
    idx = rea.guide("")
    assert "Work summary-first" in idx
    js = rea.guide("javascript electron feature trace")
    assert "javascript-applications" in js and len(js) <= 6200


def test_rea_static_mode_filters_runtime_tools(tmp_path):
    r = make(tmp_path)
    try:
        assert r.kind["binary_overview"] == "lectura"
        for bad in ("start_frida_session", "inspect_web_page", "sneaky", "rea_self_update"):
            assert r.kind[bad] == "bloqueada"
        assert r.kind["annotate_native_function"] == "aprobar"
        cat = r.search("binary overview")
        assert "binary_overview(path*)" in cat and "frida" not in cat
        assert "PIE" in r.call("binary_overview", {"path": "a.out"})
        try:
            r.call("start_frida_session", {})
            raise AssertionError("debería bloquear")
        except MCPError:
            pass
    finally:
        r.close()


def test_rea_requires_jail_without_bwrap(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: None)
    ws = tools.Workspace(str(tmp_path))
    try:
        rea.REATools(ws, command=["true"])
        raise AssertionError("sin bwrap no debe arrancar")
    except MCPError:
        pass


def test_jarvis_needs_approval_and_offline_message(monkeypatch):
    assert "jarvis" in tools.NEEDS_APPROVAL
    from tisma_code import jarvis
    monkeypatch.setattr(jarvis, "ENDPOINT", "http://127.0.0.1:9")
    assert "no está en marcha" in jarvis.ask("hola")
