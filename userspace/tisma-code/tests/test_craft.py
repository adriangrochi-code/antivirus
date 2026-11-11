"""Craft Anything vía MCP con un servidor falso que imita su protocolo (anotaciones y bandeja)."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import craft, harness, tools  # noqa: E402
from tisma_code.mcp_client import MCPError  # noqa: E402

FAKE = r'''
import json, os, sys, time
args = sys.argv[1:]
inbox = args[args.index("--inbox") + 1] if "--inbox" in args else None
deep = "--deep" in args
RO = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
EV = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
NAT = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
RUN = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}
TOOLS = [
 {"name": "core_target_triage", "title": "What is this?", "description": "Identify a target", "annotations": EV,
  "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
 {"name": "core_finding_record", "title": "Record a finding", "description": "Finding with evidence ids", "annotations": EV},
 {"name": "native_functions_list", "title": "List functions", "description": "Functions", "annotations": NAT},
 {"name": "web_site_capture", "title": "Capture a website", "description": "Capture for rebuilding", "annotations": NAT},
 {"name": "web_pixels_compare", "title": "Compare two screenshots", "description": "Pixel diff", "annotations": RO},
 {"name": "runtime_walkthrough_record", "title": "Run and watch a program", "description": "x", "annotations": RUN},
 {"name": "engines_install", "title": "Install an engine", "description": "x", "annotations": NAT},
 {"name": "rebuild_start", "title": "Rebuild", "description": "cloud agent", "annotations": NAT},
 {"name": "core_person_ask", "title": "Ask the person", "description": "x", "annotations": EV},
]
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    if m["method"] == "initialize": r = {"protocolVersion": "2025-06-18", "capabilities": {}}
    elif m["method"] == "tools/list": r = {"tools": TOOLS}
    elif m["method"] == "tools/call":
        name = m["params"]["name"]
        text = name + " deep=" + str(deep) + " " + json.dumps(m["params"]["arguments"])
        if name == "runtime_walkthrough_record":
            rid = "req-" + str(m["id"])
            req = {"id": rid, "kind": "approval", "title": "Allow", "action": "tool:" + name, "created_ms": 0}
            open(os.path.join(inbox, rid + ".request.json"), "w").write(json.dumps(req))
            ans = os.path.join(inbox, rid + ".answer.json")
            for _ in range(100):
                if os.path.exists(ans): break
                time.sleep(0.05)
            a = json.load(open(ans)) if os.path.exists(ans) else {}
            text = "ran" if a.get("allow") else "the person declined this action"
        r = {"content": [{"type": "text", "text": text}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


def make(tmp_path, **kw):
    srv = tmp_path / "fake_craft.py"
    srv.write_text(FAKE)
    ws = tools.Workspace(str(tmp_path))
    return craft.CraftTools(ws, command=[sys.executable, str(srv)], jail=False, **kw)


def test_clean_room_default_filters_and_reads(tmp_path):
    c = make(tmp_path)
    try:
        assert c.kind["core_target_triage"] == "lectura" and c.kind["native_functions_list"] == "lectura"
        assert c.kind["web_pixels_compare"] == "lectura"            # comparar capturas no usa la red
        for bad in ("web_site_capture", "runtime_walkthrough_record", "engines_install", "rebuild_start",
                    "core_person_ask"):
            assert c.kind[bad] == "bloqueada", bad
        cat = c.search("compare screenshots")
        assert "web_pixels_compare" in cat and "limpio" in cat
        assert "deep=False" in c.call("core_target_triage", {"path": "/x"})
        for bad, msg in (("runtime_walkthrough_record", "ejecucion"), ("web_site_capture", "red"),
                         ("rebuild_start", "nube")):
            try:
                c.call(bad, {})
                raise AssertionError("debería bloquear " + bad)
            except MCPError as e:
                assert msg in str(e)
    finally:
        c.close()


def test_deep_and_network_modes(tmp_path):
    c = make(tmp_path, mode="profundo", network=True)
    try:
        assert "deep=True" in c.call("core_target_triage", {"path": "/x"})
        assert c.kind["web_site_capture"] == "aprobar"
        assert c.kind["runtime_walkthrough_record"] == "bloqueada"
        assert c.kind["engines_install"] == "bloqueada"
    finally:
        c.close()


def test_execution_mode_approves_only_the_current_call(tmp_path):
    c = make(tmp_path, mode="ejecucion")
    try:
        assert c.kind["runtime_walkthrough_record"] == "aprobar" and c.needs_approval("runtime_walkthrough_record")
        assert c.call("runtime_walkthrough_record", {}) == "ran"
        # Una petición que no corresponde a la llamada aprobada se deniega.
        req = {"id": "otra", "kind": "approval", "action": "tool:runtime_walkthrough_record", "created_ms": 0}
        (c.inbox / "otra.request.json").write_text(json.dumps(req))
        c.answer_pending()
        assert json.loads((c.inbox / "otra.answer.json").read_text()) == {"allow": False, "always": False}
    finally:
        c.close()


def test_inbox_answer_does_not_follow_symlinks(tmp_path):
    c = make(tmp_path, mode="ejecucion")
    try:
        c._stop.set()                                  # sin el hilo: control manual
        victim = tmp_path / "victima.txt"
        victim.write_text("intacto")
        os.symlink(victim, c.inbox / ".evil.tmp")
        os.symlink(victim, c.inbox / "x1.answer.json")  # ya «respondida»: se ignora
        (c.inbox / "x2.request.json").write_text(json.dumps({"kind": "question", "title": "?"}))
        (c.inbox / "Mal_ID.request.json").write_text("{}")
        assert c.answer_pending() == 1
        assert victim.read_text() == "intacto"
        assert "chat" in json.loads((c.inbox / "x2.answer.json").read_text())["text"]
        assert not (c.inbox / "Mal_ID.answer.json").exists()
    finally:
        c.close()


def test_requires_jail_and_tool_registration(tmp_path, monkeypatch):
    for n in ("craft", "craft_buscar", "craft_guia", "craft_modo"):
        assert n in tools.TOOL_NAMES
    assert "craft" in harness.UNTRUSTED_TOOLS
    assert "Verificar" in craft.guide() and "evidencia" in craft.guide()
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: None)
    try:
        craft.CraftTools(tools.Workspace(str(tmp_path)), command=["true"])
        raise AssertionError("sin bwrap no debe arrancar")
    except MCPError:
        pass
