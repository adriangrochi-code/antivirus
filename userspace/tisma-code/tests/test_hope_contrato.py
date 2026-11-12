"""Contrato TISMA Code ↔ red de Hope (acordado con la sesión de Hope): sintaxis ⟦⟧, sesión para su caché,
metadatos hope_modelo.json, lanzamiento aislado y trazas opt-in. Sin código de Hope."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import agent, harness, hope_motor, models, tools, trazas  # noqa: E402

SCHEMAS = list(tools.TOOL_SCHEMAS) + list(tools.HOPE_SCHEMAS)
A275 = {"ram_mb": 15600, "vram_mb": 0, "gpus": [], "ram_bandwidth_gbs": 17.0}
GTX1060 = {"ram_mb": 16000, "vram_mb": 6144, "gpus": [{"name": "NVIDIA GeForce GTX 1060 6GB", "vram_mb": 6144}]}


# ---------------------------------------------------------------- sintaxis ⟦nombre: entrada⟧
def test_corchetes_van_a_las_herramientas_del_agente():
    calls = harness.extract_bracket_tool_calls("Leo ⟦read_file: src/main.py⟧ y busco ⟦search: def suma⟧", SCHEMAS)
    assert [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls] == [
        ("read_file", {"path": "src/main.py"}), ("search", {"pattern": "def suma"})]


def test_corchetes_con_json_y_catalogo_de_hope():
    txt = '⟦read_file: {"path": "a.py", "offset": 10}⟧ ⟦calc: 11 V / 0,05 A en ohm⟧ ⟦inventada_x: 1⟧'
    calls = harness.extract_bracket_tool_calls(txt, SCHEMAS, hope_catalog=True)
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.py", "offset": 10}
    assert calls[1]["function"]["name"] == "hope"
    assert json.loads(calls[1]["function"]["arguments"]) == {"nombre": "calc", "entrada": "11 V / 0,05 A en ohm"}
    assert len(calls) == 3          # sin catálogo instalado, «inventada_x» no se ejecutaría (ver abajo)
    assert len(harness.extract_bracket_tool_calls(txt, SCHEMAS, hope_catalog=False)) == 1


class LLM:
    def __init__(self, script):
        self.script, self.extras = list(script), []

    def __call__(self, endpoint, model, messages, schemas, extra=None):
        self.extras.append(extra)
        return (self.script.pop(0) if self.script else {"content": "Listo."}), {"prompt_tokens": 50,
                                                                               "completion_tokens": 5}


def _sesion(tmp_path, modelo, script):
    (tmp_path / "proy").mkdir()
    (tmp_path / "proy" / "calc.py").write_text("def suma(a, b):\n    return a + b\n")
    llm = LLM(script)
    s = agent.Session(str(tmp_path / "proy"), "http://127.0.0.1:9/v1", modelo, llm=llm,
                      kb_dir=tmp_path / "kb", auto_verify=False, especialistas=False)
    s.send("¿Qué hace calc.py?")
    ev = []
    while not s.events.empty():
        ev.append(s.events.get())
    return s, llm, ev


def test_sesion_con_hope_ejecuta_corchetes_y_manda_su_sesion(tmp_path):
    s, llm, ev = _sesion(tmp_path, "hope-hn1-1b", [{"content": "Veamos ⟦read_file: calc.py⟧"}, {"content": "Suma."}])
    leidas = [e for e in ev if e["type"] == "tool" and e["tool"] == "read_file"]
    assert leidas and leidas[0]["ok"] and "return a + b" in leidas[0]["result"]
    # Mismo id en todas las peticiones de la sesión (caché de conversación de Hope)
    assert {e["user"] for e in llm.extras if e} == {s.sesion_id} and len(s.sesion_id) == 32


def test_otros_modelos_no_interpretan_corchetes(tmp_path):
    _, llm, ev = _sesion(tmp_path, "qwen3-8b", [{"content": "Sería ⟦read_file: calc.py⟧"}])
    assert not [e for e in ev if e["type"] == "tool"]
    assert all(e is None for e in llm.extras)          # sin «user» para motores que no son Hope


def test_cabecera_de_sesion_solo_con_un_id_valido(monkeypatch):
    vistos = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def abrir(req, timeout=0):
        vistos.append(dict(req.header_items()))
        return R()
    monkeypatch.setattr(agent, "_check_local_owner", lambda ep: None)
    monkeypatch.setattr(agent.urllib.request, "urlopen", abrir)
    monkeypatch.setattr(agent.json, "load", lambda r: json.loads(r.read()))
    agent._chat_completion("http://127.0.0.1:1/v1", "hope", [], None, extra={"user": "a" * 32})
    agent._chat_completion("http://127.0.0.1:1/v1", "hope", [], None, extra={"user": "x\r\nInyectada: 1"})
    assert vistos[0].get("X-hope-session") == "a" * 32
    assert "X-hope-session" not in vistos[1]


# ---------------------------------------------------------------- hope_modelo.json
def meta(**kw):
    d = {"version_esquema": 1, "preset": "hn1-1b", "parametros_totales": 946_000_000, "activos_por_byte": 300_000_000,
         "flops_por_byte": 6e8, "bytes_por_token_equiv": 4, "kv_bytes_por_byte_contexto": 2048,
         "contexto_max_bytes": 262144, "cuantizacion": 4, "hash": "abc", "linaje": "qwen3-0.6b bytificado",
         "licencia_pesos": "apache-2.0",
         "dispositivos": [{"tipo": "cuda", "vram_min_gb": 4, "tok_s_medido": None},
                          {"tipo": "xpu", "vram_min_gb": 8, "tok_s_medido": 55.0},
                          {"tipo": "cpu", "vram_min_gb": 0, "tok_s_medido": None}]}
    d.update(kw)
    return d


def escribir(tmp_path, nombre, d):
    c = tmp_path / nombre
    c.mkdir(parents=True)
    (c / hope_motor.META).write_text(json.dumps(d))
    return c


def test_metadatos_validos_e_invalidos(tmp_path):
    m = hope_motor.leer_meta(escribir(tmp_path, "ok", meta()))
    assert m["cuantizacion"] == 4 and [d["tipo"] for d in m["dispositivos"]] == ["cuda", "xpu", "cpu"]
    for malo in (meta(version_esquema=2), meta(cuantizacion=0), meta(activos_por_byte=10 ** 12),
                 meta(contexto_max_bytes="mucho"), meta(dispositivos=[{"tipo": "tpu"}])):
        with pytest.raises(hope_motor.MetaError):
            hope_motor.leer_meta(escribir(tmp_path, f"m{id(malo)}", malo))
    listados = hope_motor.modelos_locales(tmp_path)
    assert any("meta" in x for x in listados) and any("error" in x for x in listados)


def _m():
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        return hope_motor.leer_meta(escribir(Path(t), "x", meta()))


def test_plan_gtx1060_cuda_a275_cpu_y_arc_xpu():
    m = _m()
    p = hope_motor.plan(m, GTX1060, arc=False)
    assert p["dispositivo"] == "cuda" and 4096 <= p["contexto_bytes"] <= 262144
    p = hope_motor.plan(m, A275, arc=False)
    assert p["dispositivo"] == "cpu"
    assert hope_motor.estimar_velocidad(m, p, A275) > 0
    p = hope_motor.plan(m, A275, arc=True)
    assert p["dispositivo"] == "xpu" and hope_motor.estimar_velocidad(m, p, A275) == 55.0   # lo medido manda
    # GPU con menos VRAM de la que pide el modelo: no se usa
    chica = {**GTX1060, "gpus": [{"name": "NVIDIA GeForce GT 1030", "vram_mb": 2048}]}
    assert hope_motor.plan(m, chica, arc=False)["dispositivo"] == "cpu"


def test_servidor_solo_local_aislado_y_dentro_de_la_carpeta(tmp_path, monkeypatch):
    monkeypatch.setattr(hope_motor, "HOPE_DIR", tmp_path / "hope")
    c = escribir(tmp_path / "hope", "hn1", meta())
    srv = hope_motor.HopeServer()
    srv.port = 4321
    argv = srv.argv(str(c), {"dispositivo": "cpu", "contexto_bytes": 65536})
    assert "-I" in argv and argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--api-key") + 1] == srv.api_key and "serve" in argv
    with pytest.raises(ValueError):
        srv.argv(str(escribir(tmp_path, "fuera", meta())), {"dispositivo": "cpu", "contexto_bytes": 4096})
    if hope_motor.shutil.which("bwrap"):
        jail = models.jail_engine(argv)
        assert "--cap-drop" in jail and "--unshare-all" in jail


def test_sin_hope_instalado_no_arranca(tmp_path, monkeypatch):
    monkeypatch.setattr(hope_motor, "instalado", lambda vendor=None: False)
    with pytest.raises(RuntimeError):
        hope_motor.HopeServer().start(str(tmp_path), A275)


# ---------------------------------------------------------------- trazas (opt-in, locales)
def test_trazas_esquema_permisos_y_licencias(tmp_path, monkeypatch):
    proy = tmp_path / "proy"
    proy.mkdir()
    (proy / "LICENSE").write_text("MIT License\n\nPermission is hereby granted, free of charge, to any person")
    modelos = tmp_path / "models"
    (modelos / "Qwen__Qwen3-8B-GGUF").mkdir(parents=True)
    (modelos / "Qwen__Qwen3-8B-GGUF" / "licencia.json").write_text(
        json.dumps({"licencia": "apache-2.0", "archivos": ["Qwen3-8B-Q4_K_M.gguf"]}))
    monkeypatch.setattr(models, "MODELS_DIR", modelos)
    t = trazas.Trazas(proy, dir=tmp_path / "trazas")
    t.paso("arreglar suma", "read_file", {"path": "calc.py"}, "x" * 30000, True, "Qwen3-8B-Q4_K_M.gguf")
    t.paso("arreglar suma", "search", {"pattern": "a"}, "nada", False, "modelo-raro.gguf")
    f = t.archivo()
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    a, b = [json.loads(x) for x in f.read_text().splitlines()]
    assert set(a) == {"tarea", "herramienta", "entrada", "salida", "ok", "modelo_generador", "licencia_modelo",
                      "repo_licencia"}
    assert a["licencia_modelo"] == "apache-2.0" and a["repo_licencia"] == "mit" and len(a["salida"]) < 21000
    assert b["licencia_modelo"] == "desconocida" and b["ok"] is False


def test_trazas_desactivadas_por_defecto(tmp_path):
    s, _, _ = _sesion(tmp_path, "hope-hn1", [{"content": "⟦read_file: calc.py⟧"}])
    assert s.trazas is None


def test_licencia_de_hugging_face():
    assert models.licencia_de({"cardData": {"license": "Apache-2.0"}}) == "apache-2.0"
    assert models.licencia_de({"tags": ["gguf", "license:mit"]}) == "mit"
    assert models.licencia_de({"cardData": {"license": "<script>"}}) == "desconocida"
    assert models.licencia_de({}) == "desconocida"
