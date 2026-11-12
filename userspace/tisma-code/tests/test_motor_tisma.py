"""Motor TISMA: lectura GGUF, plan por equipo, medición y elección de motor y modelo."""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import models, motor  # noqa: E402

GB = 1 << 30
A275 = {"ram_mb": 15600, "vram_mb": 0, "gpus": [], "ram_bandwidth_gbs": 17.0}
RTX8 = {"ram_mb": 32000, "vram_mb": 8192, "gpus": [{"name": "RTX 4060", "vram_mb": 8192}]}


def s(x: str) -> bytes:
    b = x.encode()
    return struct.pack("<Q", len(b)) + b


def write_gguf(path, kv: dict, tensors: list[tuple[str, int]]):
    """GGUF v3 mínimo: kv (int → u32, str, list[str]) y tensores (nombre, bytes) seguidos."""
    out = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(kv))
    for k, v in kv.items():
        out += s(k)
        if isinstance(v, str):
            out += struct.pack("<I", 8) + s(v)
        elif isinstance(v, list):
            out += struct.pack("<IIQ", 9, 8, len(v)) + b"".join(s(t) for t in v)
        else:
            out += struct.pack("<II", 4, v)
    off = 0
    for name, size in tensors:
        out += s(name) + struct.pack("<I", 2) + struct.pack("<QQ", 16, 16) + struct.pack("<I", 0) + struct.pack("<Q", off)
        off += size
    out += b"\0" * (-len(out) % 32)
    Path(path).write_bytes(out + b"\1" * off)


def moe_file(path, vocab=("a", "b", "c"), scale=1):
    kv = {"general.architecture": "qwen3moe", "general.name": "Prueba MoE", "qwen3moe.block_count": 2,
          "qwen3moe.expert_count": 8, "qwen3moe.expert_used_count": 2, "qwen3moe.context_length": 40960,
          "qwen3moe.attention.head_count": 8, "qwen3moe.attention.head_count_kv": 2,
          "qwen3moe.attention.key_length": 64, "tokenizer.ggml.model": "gpt2", "tokenizer.ggml.tokens": list(vocab)}
    tensors = [(n, b * scale) for n, b in (("token_embd.weight", 2000), ("blk.0.attn_q.weight", 1000),
               ("blk.0.ffn_gate_exps.weight", 8000), ("blk.0.ffn_down_shexp.weight", 500), ("blk.1.ffn_up_exps.weight", 8000))]
    write_gguf(path, kv, tensors)


def test_gguf_reader(tmp_path):
    moe_file(tmp_path / "m.gguf")
    i = motor.gguf_info(tmp_path / "m.gguf")
    assert (i["arquitectura"], i["capas"], i["expertos"], i["expertos_activos"]) == ("qwen3moe", 2, 8, 2)
    assert (i["kv_heads"], i["key_len"], i["vocabulario"], i["contexto_max"]) == (2, 64, 3, 40960)
    assert i["bytes_expertos"] == 16000 and i["bytes_resto"] == 3500       # el experto compartido no cuenta
    assert motor.active_bytes(i, 19500) == pytest.approx(3500 + 16000 * 2 / 8)
    (tmp_path / "x.gguf").write_bytes(b"NOPE" + b"\0" * 64)
    with pytest.raises(ValueError):
        motor.gguf_info(tmp_path / "x.gguf")


INFO_MOE = {"capas": 48, "expertos": 128, "expertos_activos": 8, "contexto_max": 262144, "kv_heads": 4,
            "key_len": 128, "value_len": 0, "bytes_expertos": int(16.5 * GB), "bytes_resto": int(1.5 * GB)}
INFO_DENSO = {"capas": 32, "expertos": 0, "expertos_activos": 0, "contexto_max": 32768, "kv_heads": 8,
              "key_len": 128, "value_len": 0, "bytes_expertos": 0, "bytes_resto": int(4.7 * GB)}


def test_plans(monkeypatch):
    monkeypatch.setattr(motor, "has_igpu", lambda: True)
    p = motor.plan(INFO_DENSO, int(4.7 * GB), A275)                   # ThinkPad A275: CPU, 32k cabe
    assert p["dispositivo"] == "cpu" and "--device" in p["args"] and p["ctx"] == 32768
    p = motor.plan(INFO_MOE, 18 * GB, RTX8)                            # MoE de 18 GB con GPU de 8 GB
    assert p["dispositivo"] == "mixto" and "--n-cpu-moe" in p["args"]
    n = int(p["args"][p["args"].index("--n-cpu-moe") + 1])
    assert 0 < n < 48 and "en la GPU" in p["motivo"]
    p = motor.plan(INFO_DENSO, int(4.7 * GB), RTX8)                    # cabe entero
    assert p == {**p, "dispositivo": "gpu", "args": ["-ngl", "999"]}
    p = motor.plan(INFO_DENSO, 10 * GB, {"ram_mb": 32000, "vram_mb": 6144, "gpus": [{"name": "x"}]})
    ngl = int(p["args"][1])
    assert p["dispositivo"] == "mixto" and 0 < ngl < 32
    p = motor.plan(INFO_DENSO, 12 * GB, A275)                          # casi sin RAM libre: contexto menor
    assert p["ctx"] < 32768


def test_candidates(monkeypatch):
    monkeypatch.setattr(motor, "has_igpu", lambda: True)
    base = motor.plan(INFO_DENSO, int(4.7 * GB), A275)
    c = motor.candidates(INFO_DENSO, int(4.7 * GB), A275, base, draft="/m/d.gguf")
    assert [x["nombre"] for x in c] == ["plan", "gráfica integrada", "CPU + gráfica para el prompt",
                                         "n-gramas (sin coste de memoria)", "con borrador"]
    assert "--device" not in c[2]["args"]                     # sin --device none: el prompt puede ir a la gráfica
    assert c[3]["args"][-2:] == ["--spec-type", "ngram-mod"]
    assert c[4]["args"][-6:] == ["-md", "/m/d.gguf", "--draft-max", "16", "--draft-min", "2"]
    mtp = motor.candidates({**INFO_DENSO, "mtp": 1}, int(4.7 * GB), A275, base)
    assert any(x["args"][-2:] == ["--spec-type", "draft-mtp"] for x in mtp)
    base = motor.plan(INFO_MOE, 18 * GB, RTX8)
    assert len(motor.candidates(INFO_MOE, 18 * GB, RTX8, base)) == 4


@pytest.fixture
def modeldir(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(motor, "MEDIDAS", tmp_path / "motor.json")
    monkeypatch.setattr(motor, "has_igpu", lambda: True)
    monkeypatch.setattr(motor, "thread_options", lambda: [(2, 4), (4, 2)])
    d = tmp_path / "models" / "org__m"
    d.mkdir(parents=True)
    moe_file(d / "grande.gguf", scale=20)
    moe_file(d / "borrador.gguf")                                       # mismo tokenizador, 20 veces menor
    moe_file(d / "otro.gguf", vocab=("x", "y"))                         # otro vocabulario: no sirve de borrador
    return d


def test_optimize_picks_fastest_and_remembers(modeldir):
    speeds = {"plan": 5.0, "gráfica integrada": 3.2, "con borrador": 7.5}
    seen = []

    def run(path, hw, args, ctx, threads):
        name = "con borrador" if "-md" in args else "gráfica integrada" if "--device" not in args else "plan"
        seen.append((name, threads))
        return (speeds[name] + (0.4 if threads == (4, 2) else 0), 40.0)
    rec = motor.optimize(str(modeldir / "grande.gguf"), A275, run=run)
    assert rec["nombre"] == "con borrador" and rec["hilos"] == [4, 2] and rec["gen"] == 7.9
    assert seen[-1] == ("con borrador", (4, 2))                         # los hilos se prueban sobre la mejor
    cfg = motor.config_for(str(modeldir / "grande.gguf"), A275)
    assert cfg["medido"] and cfg["args"][cfg["args"].index("-md") + 1].endswith("borrador.gguf") and cfg["hilos"] == (4, 2)
    assert not motor.config_for(str(modeldir / "grande.gguf"), RTX8)["medido"]   # otro equipo: se replanifica


def test_choose_model_and_engine(modeldir, monkeypatch):
    local = [{"path": "/m/denso8b.gguf", "size": int(4.7 * GB)}, {"path": "/m/moe30b.gguf", "size": 10 * GB},
             {"path": "/m/denso14b.gguf", "size": 9 * GB}]
    speed = {"/m/denso8b.gguf": 2.2, "/m/moe30b.gguf": 6.5, "/m/denso14b.gguf": 1.1}
    monkeypatch.setattr(motor, "estimate_speed", lambda p, hw: speed[p])
    assert motor.choose_model(local, A275)["path"] == "/m/moe30b.gguf"     # el MoE: más calidad y usable
    speed["/m/moe30b.gguf"] = 3.0
    assert motor.choose_model(local, A275)["path"] == "/m/moe30b.gguf"     # ninguno usable: el más rápido
    monkeypatch.setattr(motor, "strata_installed", lambda: True)
    assert motor.choose_engine(A275, False)["motor"] == "llama.cpp"
    assert motor.choose_engine({"ram_mb": 32000, "vram_mb": 12288, "gpus": [{"name": "x"}]}, False)["abrir"]
    assert motor.choose_engine(A275, True)["motor"] == "strata"
    monkeypatch.setattr(motor, "strata_installed", lambda: False)          # no viene en la ISO: se ofrece
    big = motor.choose_engine({"ram_mb": 32000, "vram_mb": 12288, "gpus": [{"name": "x"}]}, False)
    assert big["motor"] == "llama.cpp" and big["instalar"] == "strata" and "Componentes → Strata" in big["motivo"]
    assert "sudo" not in big["motivo"]
    assert "instalar" not in motor.choose_engine(A275, False)               # el A275 no puede con Strata


def test_local_server_uses_motor_plan(modeldir, monkeypatch):
    monkeypatch.setattr(models.shutil, "which", lambda n: "/bin/llama-server")
    srv = models.LocalServer()
    spawned = []
    monkeypatch.setattr(srv, "_spawn", lambda argv: spawned.append(argv) or type("P", (), {
        "poll": lambda self: None, "wait": lambda self, t=None: 0, "terminate": lambda self: None})())
    monkeypatch.setattr(srv, "_fallback", lambda *a: None)
    srv.start(str(modeldir / "grande.gguf"), A275)
    argv = spawned[0]
    assert argv[argv.index("--device") + 1] == "none" and argv[argv.index("-c") + 1] == "32768"
    with pytest.raises(ValueError, match="borrador"):
        srv.start(str(modeldir / "grande.gguf"), A275, extra=["-md", "/etc/passwd"])


def test_choice_by_agent_step_time_and_quant_recommendation():
    slow_prompt = {"gen": 5.0, "prompt": 15.0}                  # CPU sola: genera algo más rápido...
    fast_prompt = {"gen": 4.6, "prompt": 90.0}                  # ...pero la gráfica lee el prompt 6 veces más rápido
    assert motor.step_seconds(fast_prompt) < motor.step_seconds(slow_prompt)
    hw = {"ram_mb": 15600, "vram_mb": 0, "ram_bandwidth_gbs": 13.0}
    files = [{"file": n, "size": int(s * GB), "fit": models.fit(int(s * GB), hw, name=n)}
             for n, s in (("m-7B-Q4_K_M.gguf", 4.4), ("m-3B-Q4_K_M.gguf", 1.9), ("m-3B-Q8_0.gguf", 3.2),
                          ("m-30B-A3B-Q3_K_M.gguf", 13.5))]
    models.recommend(files)
    rec = [f["file"] for f in files if f.get("recomendado")]
    assert rec == ["m-3B-Q4_K_M.gguf"]                          # con ~13 GB/s, lo que va a velocidad usable
    assert files[3]["fit"]["where"] == "no"                     # el MoE de 13,5 GB no cabe con 16 GB
    assert models.active_fraction("Qwen3-30B-A3B-Q4_K_M") < 0.15


def test_engine_runs_jailed_with_models_readonly_and_gpu(modeldir, monkeypatch):
    monkeypatch.setattr(models.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    argv = models.jail_engine(["/opt/llama.cpp/bin/llama-server", "-m", "x.gguf"])
    assert argv[0] == "/usr/bin/bwrap" and argv[-3:] == ["/opt/llama.cpp/bin/llama-server", "-m", "x.gguf"]
    home = str(models.Path.home())
    assert argv[argv.index(home) - 1] == "--tmpfs"                          # sin la carpeta personal
    m = str(models.MODELS_DIR.resolve())
    i = argv.index(m)
    assert argv[i - 1] == "--ro-bind" and argv[i + 1] == m                  # modelos: solo lectura
    assert "--share-net" in argv and "--unshare-all" in argv and ["--cap-drop", "ALL"] == argv[argv.index("--cap-drop"):argv.index("--cap-drop") + 2]
    assert ["--dev-bind-try", "/dev/dri", "/dev/dri"] == argv[argv.index("/dev/dri") - 1:argv.index("/dev/dri") + 2]
    assert argv.index("--dev") < argv.index("/dev/dri")                     # la GPU después del /dev mínimo
    monkeypatch.setattr(models.shutil, "which", lambda n: None)
    with pytest.raises(RuntimeError):
        models.jail_engine(["llama-server"])                                 # sin bubblewrap no arranca
