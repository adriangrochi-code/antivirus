"""Regresiones de la auditoría de seguridad: cada prueba reproduce un ataque concreto."""
import http.client
import shutil
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import docs, tools  # noqa: E402
from tests.test_agent import call  # noqa: E402


def test_cannot_open_home_or_root_as_project():
    for bad in (str(Path.home()), "/", str(Path.home() / ".ssh")):
        with pytest.raises(tools.ToolError):
            tools.Workspace(bad)


@pytest.mark.skipif(not shutil.which("bwrap"), reason="sin bubblewrap")
def test_A1_project_inside_home_does_not_expose_home(tmp_path):
    proj = Path.home() / "proyecto-prueba-tisma"
    proj.mkdir(exist_ok=True)
    secret = Path.home() / ".secreto-prueba-tisma"
    secret.write_text("CLAVE-PRIVADA")
    (proj / ".git").mkdir(exist_ok=True)
    try:
        ws = tools.Workspace(str(proj))
        out = ws.run_command(f"cat {secret} || echo OCULTO; ls /run/user 2>&1 | head -1; "
                             "echo x > .git/config && echo GIT_ESCRIBIBLE || echo GIT_RO; echo $SSH_AUTH_SOCK$DBUS_SESSION_BUS_ADDRESS")
        assert "CLAVE-PRIVADA" not in out and "OCULTO" in out and "GIT_RO" in out
        assert "unix:" not in out
    finally:
        secret.unlink()
        shutil.rmtree(proj)


def test_A1_no_bwrap_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(tools.ToolError):
        tools.Workspace(str(tmp_path)).sandbox_argv("true")


def test_A2_static_path_traversal_blocked():
    from tisma_code import server
    server.APP = server.App()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_port
    try:
        for path, host in (("/static//etc/passwd", f"127.0.0.1:{port}"), ("/static/../server.py", f"127.0.0.1:{port}"),
                           ("/static/logo.svg", f"evil.example:{port}")):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path, headers={"Host": host})
            r = c.getresponse()
            body = r.read()
            assert r.status in (403, 404) and b"root:" not in body
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/static/logo.svg")
        assert c.getresponse().status == 200
    finally:
        httpd.shutdown()


def test_A3_symlinked_tisma_dir_refused(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / ".tisma").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        docs.DocSpace(proj, tmp_path / "idx", library=tmp_path / "lib")


def test_A3_symlinked_files_not_indexed_nor_searched(tmp_path):
    outside = tmp_path / "fuera.txt"
    outside.write_text("PRIVATE-KEY-XYZ")
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "notas.txt").symlink_to(outside)
    ws = tools.Workspace(str(proj))
    assert "PRIVATE-KEY-XYZ" not in ws.search("PRIVATE")
    with pytest.raises(tools.ToolError):
        ws.read_file("notas.txt")


def test_C1_hope_path_and_forge_tools_blocked(tmp_path):
    hb = pytest.importorskip("tisma_code.hope_bridge")
    h = hb.HopeTools(tmp_path / "d", chat=None)
    for name in ("git_leer", "stl", "gcode", "crear_herramienta", "componer", "preguntar_maestro", "buscar_web"):
        assert "no está disponible" in h.run(name, "/etc/passwd")


def test_C2_verify_needs_approval_without_trust(tmp_path_factory):
    proj = tmp_path_factory.mktemp("p")
    (proj / "pytest.ini").write_text("[pytest]\n")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_x.py").write_text("def test_x():\n    open('PWNED', 'w')\n")
    (proj / "a.py").write_text("x = 1\n")
    from tisma_code import agent
    llm_script = [{"content": "", "tool_calls": [call("edit_file", path="a.py", old="x = 1", new="x = 2")]},
                  {"content": "Hecho."}]
    s = agent.Session(str(proj), "http://x/v1", "m", llm=lambda *a, **k: (llm_script.pop(0) if llm_script else {"content": "ok"}, {}),
                      kb_dir=tmp_path_factory.mktemp("kb"))
    s.always_allow.add("edit_file")
    t = threading.Thread(target=s.send, args=("cambia x",))
    t.start()
    ev = None
    for _ in range(100):
        ev = s.events.get(timeout=5)
        if ev["type"] == "approval":
            break
    assert ev and ev["type"] == "approval" and ev["tool"] == "verify"
    s.resolve_approval(ev["id"], False)
    t.join(10)
    assert not (proj / "PWNED").exists()


def test_git_tool_rejects_pager_and_abbreviated_options(tmp_path):
    from tisma_code import devtools, tools
    ws = tools.Workspace(str(tmp_path))
    for bad in (["--open-files-in-pager=sh -c 'touch /tmp/x'", "p"], ["-Osh", "p"], ["-nOsh", "p"],
                ["--open-f=sh", "p"], ["--outp=/tmp/x"], ["--output=/tmp/x"], ["--ext-diff"], ["--exec=sh"],
                ["--upload-pa=sh"]):
        try:
            devtools.git(ws, "grep" if "p" in bad else "diff", bad)
            raise AssertionError(f"debería rechazar {bad}")
        except tools.ToolError as e:
            assert "no permitida" in str(e)
    for ok in (["-n", "-i", "-e", "patrón"], ["-in", "TODO"], ["--stat", "HEAD~1"], ["-5", "--oneline"],
               ["-U3"], ["--", "-raro.txt"]):
        assert all(devtools._git_arg_ok(a) for a in ok if a != "--") or "--" in ok


def test_rea_craft_workdir_symlink_to_home_is_refused(tmp_path):
    import os
    import sys
    from tisma_code import craft, rea, tools
    from tisma_code.base import ToolError
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "repo"
    (proj / ".tisma").mkdir(parents=True)
    os.symlink(home, proj / ".tisma" / "rea")
    os.symlink(home, proj / ".tisma" / "craft")
    ws = tools.Workspace(str(proj))
    for mk in (lambda: rea.REATools(ws, command=[sys.executable, "-c", "pass"], jail=False),
               lambda: craft.CraftTools(ws, command=[sys.executable, "-c", "pass"], jail=False)):
        try:
            mk()
            raise AssertionError("debería rechazar el enlace")
        except ToolError as e:
            assert "enlace" in str(e)


def test_docs_subdir_symlink_is_refused(tmp_path):
    import os
    from tisma_code.base import ToolError
    from tisma_code.docs import DocSpace
    outside = tmp_path / "fuera"
    outside.mkdir()
    proj = tmp_path / "repo"
    (proj / ".tisma").mkdir(parents=True)
    os.symlink(outside, proj / ".tisma" / "mejoras")
    try:
        DocSpace(proj, tmp_path / "idx", library=tmp_path / "lib")
        raise AssertionError("debería rechazar el enlace")
    except ToolError:
        pass
    assert list(outside.iterdir()) == []
