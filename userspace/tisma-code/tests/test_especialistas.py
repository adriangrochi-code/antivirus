"""Modelos especialistas: configuración segura, verificadores, delegación y roles."""
import json
import os
import sys
import time
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
                {"nombre": "z", "tipo": "programa", "descripcion": ""},
                {"nombre": "nube", "tipo": "llm", "descripcion": "", "endpoint": "https://api.example.com/v1"},
                {"nombre": "truco", "tipo": "llm", "descripcion": "", "endpoint": "http://127.0.0.1@evil.example/v1"}):
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
    # Último uso hace más del límite (no «0.0»: en un equipo recién arrancado, como los de la CI, el reloj
    # monótono todavía no llegó a IDLE_UNLOAD y el modelo parecía recién usado)
    pool._servers["x"], pool._last_use["x"] = srv, time.monotonic() - esp.IDLE_UNLOAD - 1
    assert pool.unload_idle() == ["x"] and srv.stopped and not pool._servers


def test_catalog_only_open_source_and_pinned():
    c = esp.catalog()
    ids = set()
    for e in c["instalables"]:
        assert e["licencia"] in ("Apache-2.0", "MIT"), e["id"]
        assert len(e["sha256"]) == 64 and int(e["sha256"], 16) >= 0
        assert e["archivo"].endswith(".gguf") and e["mb"] < 2500        # cabe junto al modelo principal en 16 GB
        assert esp.NAME_RE.match(e["id"]) and e["id"] not in ids
        ids.add(e["id"])
    for e in c["con_otro_motor"]:
        assert e["licencia"] in ("Apache-2.0", "MIT")
    assert any("NC" in e["licencia"] for e in c["no_recomendados"])


def test_install_from_catalog(tmp_path, monkeypatch):
    from tisma_code import models
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path / "models")
    cfg = tmp_path / "cfg" / "especialistas.json"
    entry, gguf = esp.install("resumidor", cfg)
    assert gguf.endswith("Qwen__Qwen3-1.7B-GGUF/Qwen3-1.7B-Q8_0.gguf")
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    esp.install("extractor-json", cfg)
    specs, roles = esp.load(cfg)
    assert set(specs) == {"resumidor", "extractor-json"} and roles == {"compactar": "resumidor"}
    assert specs["extractor-json"].verificador == "json"
    pool = esp.Pool(llm=None, path=cfg)
    try:
        pool.ask("resumidor", "x")
        raise AssertionError("sin descargar no debe arrancar")
    except esp.SpecialistError as e:
        assert "descargado" in str(e)
    try:
        esp.install("no-existe", cfg)
        raise AssertionError
    except esp.SpecialistError:
        pass


def test_download_rejects_wrong_sha(tmp_path, monkeypatch):
    from tisma_code import models
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path)
    d = models.Downloads()

    def fake_fetch(model_id, part, dest, st):
        dest.write_bytes(b"modelo manipulado")
    monkeypatch.setattr(d, "_fetch", fake_fetch)
    d.state["k"] = {"key": "k", "status": "descargando", "done": 0, "total": 0}
    d._run("a/b", "m.gguf", "k", "0" * 64)
    assert d.state["k"]["status"] == "error" and "SHA-256" in d.state["k"]["error"]
    assert not (tmp_path / "a__b" / "m.gguf").exists()


def test_threads_choice(tmp_path, monkeypatch):
    from tisma_code import models
    sysf, med = tmp_path / "hilos-sistema.json", tmp_path / "hilos.json"
    sysf.write_text(json.dumps({"ia_generacion": 2, "ia_lote": 4}))
    monkeypatch.setattr(models, "HILOS_SISTEMA", sysf)
    monkeypatch.setattr(models, "HILOS_MEDIDOS", med)
    assert models.threads_for("x.gguf") == (2, 4)                    # topología de centinela
    med.write_text(json.dumps({"y.gguf": {"t": 4, "tb": 2}}))          # medición antigua por modelo
    assert models.threads_for("y.gguf") == (4, 2)


def test_local_server_kv_quant_with_fallback(tmp_path, monkeypatch):
    import subprocess as sp
    from tisma_code import models
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path)
    (tmp_path / "a__b").mkdir()
    g = tmp_path / "a__b" / "m.gguf"
    g.write_bytes(b"0" * 1000)
    monkeypatch.setattr(models.shutil, "which", lambda n: "/bin/false")
    spawned = []

    def fake_spawn(argv):
        spawned.append(argv)
        # primero falla (sin soporte de Flash Attention); el relanzamiento sin compresión sigue vivo
        return sp.Popen(["false"] if len(spawned) == 1 else ["sleep", "5"])
    srv = models.LocalServer()
    monkeypatch.setattr(srv, "_spawn", fake_spawn)
    monkeypatch.setattr(models, "threads_for", lambda f: None)
    srv.start(str(g), {"ram_mb": 16000, "vram_mb": 0})
    assert "-ctv" in spawned[0] and "q8_0" in spawned[0]
    import time
    for _ in range(50):
        if len(spawned) == 2:
            break
        time.sleep(0.05)
    assert len(spawned) == 2 and "-ctv" not in spawned[1] and srv.kv_quant is False
    srv.stop()
