"""SolidCraft vía MCP con un servidor falso: permisos (guardar/exportar) y verificación con expect."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import solidcraft, tools  # noqa: E402

FAKE = r'''
import json, sys
args = sys.argv[1:]
assert args[:2] == ["mcp", "--headless"] and args[2] == "--allow-path"
TOOLS = [{"name": n, "description": d, "inputSchema": {"type": "object"}} for n, d in [
    ("execute", "Run any command"), ("batch", "Run steps"), ("measure", "Area and volume"),
    ("set_parameter", "Create or change a user parameter"), ("export", "STEP, IGES, 3MF or mesh export"),
    ("save", "Save the design"), ("new_design", "Replace the design")]]
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    if m["method"] == "initialize": r = {"protocolVersion": "2025-06-18", "capabilities": {}}
    elif m["method"] == "tools/list": r = {"tools": TOOLS}
    else:
        p = m["params"]
        exp = p["arguments"].get("expect")
        if exp and abs(exp.get("volume_mm3", 0) - 8000.0) > 1:
            r = {"isError": True, "content": [{"type": "text", "text": "volume: measured 8000, expected %s" % exp["volume_mm3"]}]}
        else:
            r = {"content": [{"type": "text", "text": json.dumps({"ok": True, "tool": p["name"], "volume_mm3": 8000.0})}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


def make(tmp_path):
    srv = tmp_path / "fake_sc.py"
    srv.write_text(FAKE)
    return solidcraft.SolidCraftTools(tools.Workspace(str(tmp_path)), command=[sys.executable, str(srv)], jail=False)


def test_modeling_free_but_writes_need_approval(tmp_path):
    sc = make(tmp_path)
    try:
        assert not sc.needs_approval("execute") and not sc.needs_approval("measure")
        assert sc.needs_approval("save") and sc.needs_approval("export") and sc.needs_approval("new_design")
        assert sc.needs_approval("inexistente")
        assert "(pide permiso)" in sc.search("export step")
    finally:
        sc.close()


def test_expect_mismatch_is_reported_as_error(tmp_path):
    sc = make(tmp_path)
    try:
        ok = sc.call("execute", {"command": "solid.box", "params": {"size": [20, 20, 20]},
                                 "expect": {"volume_mm3": 8000.0}})
        assert '"volume_mm3": 8000.0' in ok
        bad = sc.call("execute", {"command": "solid.box", "expect": {"volume_mm3": 9000.0}})
        assert bad.startswith("Error:") and "expected 9000" in bad
    finally:
        sc.close()


def test_registered_and_guide():
    for n in ("solidcraft", "solidcraft_buscar", "solidcraft_guia"):
        assert n in tools.TOOL_NAMES
    assert "expect" in solidcraft.guide()
