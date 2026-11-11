"""Regresiones de la revisión independiente de TISMA Code (cada prueba, un fallo que existió)."""
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import agent, contexto, ingenieria, motor, tools  # noqa: E402


def gguf_bytes(kv_raw: bytes, n_kv: int) -> bytes:
    return b"GGUF" + struct.pack("<IQQ", 3, 0, n_kv) + kv_raw


def key(k: str) -> bytes:
    return struct.pack("<Q", len(k)) + k.encode()


def test_malicious_gguf_only_raises_valueerror(tmp_path):
    nested = key("x") + struct.pack("<I", 9) + (struct.pack("<IQ", 9, 1) * 5000) + struct.pack("<IQ", 4, 0)
    (tmp_path / "a.gguf").write_bytes(gguf_bytes(nested, 1))
    inf = key("general.architecture") + struct.pack("<I", 8) + key("llama") + \
        key("llama.block_count") + struct.pack("<Id", 12, float("inf"))
    (tmp_path / "b.gguf").write_bytes(gguf_bytes(inf, 2))
    for f in ("a.gguf", "b.gguf"):
        with pytest.raises(ValueError):
            motor.gguf_info(tmp_path / f)


def test_notebook_is_per_task_and_crossing_off_is_precise(tmp_path):
    c = contexto.Cuaderno(tmp_path)
    c.anotar("pendiente", "arreglar el parser")
    c.anotar("pendiente", "añadir tests al parser")
    with pytest.raises(ValueError):
        c.anotar("pendiente", "hecho: a")                  # antes: tachaba TODOS
    assert c.anotar("pendiente", "hecho: arreglar el parser").startswith("Pendiente tachado")
    assert c.data["notas"]["pendiente"] == ["añadir tests al parser"]
    c.fallo("test_x falló")
    c.nueva_tarea()
    assert c.render() == "(vacío)"                         # la tarea siguiente no hereda pendientes viejos


def test_recordar_respects_char_budget(tmp_path):
    c = contexto.Cuaderno(tmp_path)
    n = c.guardar_salida("docs_leer", "\n".join("x" * 500 for _ in range(400)))
    out = c.recordar(salida=n, lineas=400, limite=3000)
    assert len(out) < 3700 and "sigue con desde=" in out and out.startswith("[Fuente NO verificada")
    n = c.guardar_salida("run_command", "y" * 50000)          # una sola línea enorme
    assert "línea recortada" in c.recordar(salida=n, limite=3000)


class SimLLM:
    """Modelo que lee trozos grandes de un archivo durante 20 pasos (lo que hacía resumir en bucle)."""
    def __init__(self):
        self.steps, self.i = 0, 0

    def __call__(self, endpoint, model, messages, schemas, extra=None):
        if schemas is None:
            return {"content": "## Hecho\n- leí\n## Hallazgos\n" + "dato " * 150 +
                    "\n## Decisiones\n—\n## Descartado\n—\n## Pendiente\n- seguir"}, {}
        self.steps += 1
        pt = int((sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls", ""))) for m in messages)
                  + len(json.dumps(schemas))) / 3.5)
        if self.steps > 20:
            return {"content": "Listo."}, {"prompt_tokens": pt, "completion_tokens": 10}
        self.i += 1
        return {"content": "", "tool_calls": [{"id": f"c{self.i}", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "a.py", "offset": self.i * 40, "limit": 40})}}]}, \
            {"prompt_tokens": pt, "completion_tokens": 30}


@pytest.mark.parametrize("ctx,maximo", [(8192, 5), (16384, 3), (32768, 1)])
def test_no_compaction_loop_on_small_windows(tmp_path, ctx, maximo):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.py").write_text("".join(f"x_{i} = {i}  # comentario de relleno para que la línea sea larga.....\n"
                                       for i in range(3000)))
    s = agent.Session(str(proj), "http://x/v1", "m", llm=SimLLM(), kb_dir=tmp_path / "kb", auto_verify=False,
                      context_tokens=ctx)
    s.send("Revisa el archivo a.py")
    assert 0 < s.stats["compactions"] <= maximo or (ctx == 32768 and s.stats["compactions"] <= maximo)


def test_explorer_engine_error_does_not_kill_task(tmp_path):
    calls = []

    def llm(endpoint, model, messages, schemas, extra=None):
        calls.append(1)
        if len(calls) == 1:
            return {"content": "", "tool_calls": [{"id": "e", "type": "function", "function": {
                "name": "explorar", "arguments": json.dumps({"pregunta": "¿dónde?"})}}]}, {}
        if len(calls) == 2:
            raise agent.LLMError("el motor respondió 400")
        return {"content": "Hecho igualmente."}, {}
    (tmp_path / "p").mkdir()
    s = agent.Session(str(tmp_path / "p"), "http://x/v1", "m", llm=llm, kb_dir=tmp_path / "kb", auto_verify=False)
    s.send("Busca algo")
    tool = next(m for m in s.messages if m["role"] == "tool")
    assert "se interrumpió" in tool["content"] and s.messages[-1]["content"] == "Hecho igualmente."


def test_ingenieria_output_extension_and_kicad_stale_report(tmp_path, monkeypatch):
    ws = tools.Workspace(str(tmp_path))
    monkeypatch.setattr(ingenieria.shutil, "which", lambda b: "/usr/bin/" + b)
    calls = []
    monkeypatch.setattr(ws, "run_command", lambda c, timeout=120: calls.append(c) or
                        '[código de salida 0]\n{"violations": [], "unconnected_items": []}\n[stderr]\naviso')
    (tmp_path / "p.scad").write_text("cube(1);")
    with pytest.raises(tools.ToolError, match="nunca sobrescribe"):
        tools.execute(ws, "ingenieria", {"accion": "openscad", "archivo": "p.scad", "salida": "main.py"})
    (tmp_path / "x.kicad_pcb").write_text("")
    r = tools.execute(ws, "ingenieria", {"accion": "kicad_drc", "archivo": "x.kicad_pcb"})
    assert r.startswith("0 incidencia(s)") and calls[-1].startswith("rm -f .tisma/drc.json;")
    assert "Escribirá: out.stl" in tools.preview(ws, "ingenieria", {"accion": "openscad", "archivo": "p.scad",
                                                                     "salida": "out.stl"})


def test_moe_plan_counts_leading_dense_layers():
    gb = 1 << 30
    info = {"capas": 61, "capas_densas": 3, "expertos": 256, "expertos_activos": 8, "contexto_max": 163840,
            "kv_heads": 1, "key_len": 576, "value_len": 512, "bytes_expertos": int(120 * gb), "bytes_resto": int(10 * gb)}
    p = motor.plan(info, 130 * gb, {"ram_mb": 196000, "vram_mb": 24576, "gpus": [{"name": "x"}]})
    n = int(p["args"][p["args"].index("--n-cpu-moe") + 1])
    assert n > 3                                   # las 3 densas no cuentan como expertos en la GPU
