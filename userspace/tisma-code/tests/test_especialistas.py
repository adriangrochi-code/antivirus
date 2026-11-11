"""Modelos especialistas: configuración segura, verificadores, delegación y roles."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import especialistas as esp  # noqa: E402
from tisma_code.agent import Session  # noqa: E402

PUZZLE = "53..7....6..195....98....6.8...6...34..8.3..17...2...6.6....28....419..5....8..79"
SOLUTION = "534678912672195348198342567859761423426853791713924856961537284287419635345286179"

SOLVER = r'''
import json, sys
p = sys.stdin.read().strip()
print(json.dumps({"salida": "%s" if "53..7" in p else "1" * 81}))
''' % SOLUTION


def config(tmp_path, data) -> Path:
    p = tmp_path / "especialistas.json"
    p.write_text(json.dumps(data))
    p.chmod(0o600)
    return p


def test_sudoku_verifier():
    assert esp.verify_sudoku(PUZZLE, SOLUTION)[0]
    bad = "6" + SOLUTION[1:]
    ok, why = esp.verify_sudoku(PUZZLE, bad)
    assert not ok and "entrada" in why
    swapped = SOLUTION[1] + SOLUTION[0] + SOLUTION[2:]
    assert not esp.verify_sudoku("." * 81, swapped)[0]


def test_config_must_be_private_and_valid(tmp_path):
    p = config(tmp_path, {"especialistas": [{"nombre": "x", "tipo": "programa", "descripcion": "d",
                                             "comando": ["true"]}]})
    p.chmod(0o666)
    try:
        esp.load(p)
        raise AssertionError("un archivo escribible por otros no debe aceptarse")
    except esp.SpecialistError:
        pass
    p.chmod(0o600)
    assert "x" in esp.load(p)[0]
    for bad in ({"nombre": "../x", "tipo": "programa", "descripcion": "", "comando": ["true"]},
                {"nombre": "y", "tipo": "otro", "descripcion": ""},
                {"nombre": "z", "tipo": "programa", "descripcion": ""}):
        config(tmp_path, {"especialistas": [bad]})
        try:
            esp.load(p)
            raise AssertionError(bad)
        except esp.SpecialistError:
            pass
    assert esp.load(tmp_path / "no-existe.json") == ({}, {})


def test_program_specialist_is_verified(tmp_path):
    solver = tmp_path / "solver.py"
    solver.write_text(SOLVER)
    p = config(tmp_path, {"especialistas": [
        {"nombre": "sudoku", "tipo": "programa", "descripcion": "Resuelve sudokus (TRM)",
         "comando": [sys.executable, str(solver)], "verificador": "sudoku", "disparadores": ["sudoku"]}]})
    pool = esp.Pool(llm=None, path=p, jail=False)
    out = pool.ask("sudoku", PUZZLE)
    assert out.startswith("[sudoku · VERIFICADO") and SOLUTION in out
    out = pool.ask("sudoku", "." * 81)
    assert "RECHAZADO" in out
    assert pool.stats["sudoku"] == [2, 1]
    assert "Pista: el especialista sudoku" in pool.hint("Resuelve este Sudoku")
    assert pool.hint("hola") == ""
    assert "sudoku" not in pool.describe()            # catálogo bajo demanda, no en las instrucciones
    assert "sudoku (programa, verificado: sudoku · 1/2 verificadas)" in pool.search("resolver sudoku")
    assert "Ningún" in pool.search("transcribir audio")


def test_session_delegates_and_uses_roles(tmp_path):
    p = config(tmp_path, {"especialistas": [
        {"nombre": "resumidor", "tipo": "llm", "descripcion": "Resume", "endpoint": "http://127.0.0.1:9/v1",
         "modelo": "mini", "sistema": "Resume."}], "roles": {"hope": "resumidor", "compactar": "resumidor"}})
    calls = []

    def fake_llm(endpoint, model, messages, schemas):
        calls.append((model, [m["content"] for m in messages], bool(schemas)))
        if model == "mini":
            return {"content": "resumen corto"}, {}
        if sum(c[0] == "grande" for c in calls) == 1:
            return {"content": "", "tool_calls": [{"id": "c1", "function": {
                "name": "especialista", "arguments": json.dumps({"nombre": "resumidor", "entrada": "texto"})}}]}, {}
        return {"content": "Hecho."}, {}

    pool = esp.Pool(llm=fake_llm, path=p, jail=False)
    ws = tmp_path / "proj"
    ws.mkdir()
    s = Session(str(ws), "http://127.0.0.1:9/v1", "grande", llm=fake_llm, auto_verify=False,
                kb_dir=tmp_path / "kb", especialistas=pool)
    assert {"especialista", "especialista_buscar"} <= s.tool_names
    assert "resumidor" not in s.messages[0]["content"] and "especialista_buscar" in s.messages[0]["content"]
    assert s._role_chat("hope")([{"role": "user", "content": "x"}]) == "resumen corto"
    s.send("resume esto")
    assert any(c[0] == "mini" and c[1] == ["Resume.", "texto"] for c in calls)
    tool_msgs = [m for m in s.messages if m.get("role") == "tool"]
    assert tool_msgs and "NO verificado" in tool_msgs[0]["content"] and "resumen corto" in tool_msgs[0]["content"]


def test_without_config_no_specialist_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(esp, "CONFIG", tmp_path / "nada.json")
    ws = tmp_path / "p"
    ws.mkdir()
    s = Session(str(ws), "http://127.0.0.1:9/v1", "m", llm=lambda *a: ({"content": "ok"}, {}),
                auto_verify=False, kb_dir=tmp_path / "kb", especialistas=esp.Pool(None, path=tmp_path / "nada.json"))
    assert "especialista" not in s.tool_names
    assert os.path.basename(str(esp.CONFIG)) == "nada.json"


def test_idle_models_are_unloaded():
    class FakeServer:
        stopped = False

        def stop(self):
            self.stopped = True
    pool = esp.Pool(llm=None, path=Path("/nonexistent"))
    srv = FakeServer()
    pool._servers["x"], pool._last_use["x"] = srv, 0.0
    assert pool.unload_idle() == ["x"] and srv.stopped and not pool._servers
