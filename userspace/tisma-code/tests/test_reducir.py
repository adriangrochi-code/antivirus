"""«✂ Reducir»: niveles de precisión elegibles, búsqueda de capas que casi no aportan y calidad medida."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import models, reducir as r  # noqa: E402


def test_precision_y_niveles():
    assert r.precision(Path("qwen2.5-coder-1.5b-instruct-q4_k_m.gguf")) == "q4_k_m"
    assert r.precision(Path("x.gguf"), file_type=7) == "q8_0"
    assert r.niveles_posibles("q8_0") == ["normal", "liviano", "muy_liviano"]
    assert r.niveles_posibles("q4_k_m") == ["liviano", "muy_liviano"]          # nunca «subir» ni repetir
    assert r.niveles_posibles("iq3_m") == ["muy_liviano"]
    assert r.tipo_destino("q8_0", "auto") == "Q4_K_M" and r.tipo_destino("q8_0", "muy_liviano") == "IQ2_M"
    with pytest.raises(r.ReducirError):
        r.tipo_destino("q4_k_m", "normal")
    with pytest.raises(r.ReducirError):
        r.tipo_destino("q8_0", "; rm -rf /")


def test_capas_candidatas_y_eleccion():
    c = r.candidatas(28)
    assert len(c) == 6 and min(c) >= 14 and max(c) <= 25                    # ni primeras ni últimas
    assert r.candidatas(8) == []
    # Base 10: quitar 20 empeora 1 %, 22 un 3 %, 18 un 9 % (demasiado): se eligen 20 y 22
    assert r.elegir_capas(10.0, {18: 10.9, 20: 10.1, 22: 10.3}) == [20, 22]
    assert r.elegir_capas(10.0, {20: 10.35, 21: 10.38, 22: 10.39, 23: 10.39}, tope_total=0.08) == [20, 21]
    assert r.leer_ppl("...\nFinal estimate: PPL = 12.3456 +/- 0.1\n") == 12.3456


def test_jaula_sin_red_ni_carpeta_personal(tmp_path, monkeypatch):
    monkeypatch.setattr(r.shutil, "which", lambda x: "/usr/bin/bwrap")
    a = r.jaula(["llama-quantize"], tmp_path)
    assert "--unshare-all" in a and "--share-net" not in a and "--cap-drop" in a
    assert a[a.index("--tmpfs", a.index("/run")) + 1] == str(Path.home())


def test_reducir_de_punta_a_punta(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path / "models")
    d = tmp_path / "models" / "qwen"
    d.mkdir(parents=True)
    src = d / "qwen-1.5b-q8_0.gguf"
    src.write_bytes(b"x" * 1000)
    monkeypatch.setattr("tisma_code.motor.gguf_info", lambda p: {"capas": 28, "tipo_archivo": 7})
    llamadas = []

    def run(argv, trabajo):
        llamadas.append(argv)
        if argv[0] == "llama-quantize":
            Path(argv[-3]).write_bytes(b"y" * (300 if "--prune-layers" in argv else 400))
            return ""
        if argv[0] == "llama-perplexity":
            m = argv[2]
            ppl = 10.0 if m == str(src) else 10.2
            return f"Final estimate: PPL = {ppl} +/- 0.1"
        return ""
    res = r.reducir(str(src), "normal", True, run=run, trabajo_base=tmp_path / "trabajo")
    assert Path(res["archivo"]).exists() and src.exists()                  # el original no se toca
    assert res["tipo"] == "Q4_K_M" and res["capas_quitadas"] and res["calidad"] > 95
    assert all("--imatrix" in a for a in llamadas if a[0] == "llama-quantize")
    assert "--allow-requantize" in llamadas[-2]
    assert list((tmp_path / "trabajo").iterdir()) == []                    # sin restos


def test_solo_modelos_de_tisma(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path / "models")
    otro = tmp_path / "otro.gguf"
    otro.write_bytes(b"x")
    with pytest.raises(r.ReducirError):
        r.reducir(str(otro))


def test_formato_manual_solo_si_achica():
    from tisma_code import reducir as red
    assert red.manuales_posibles("q4_k_m")[0] == "IQ4_XS"                 # nunca «subir» a Q5/Q6/Q8
    assert "Q8_0" in red.manuales_posibles("f16") and "Q8_0" not in red.manuales_posibles("q8_0")
    assert red.tipo_destino("f16", "Q5_K_M") == "Q5_K_M"
    import pytest
    with pytest.raises(red.ReducirError):
        red.tipo_destino("q4_k_m", "Q6_K")


def test_nivel_de_esfuerzo(tmp_path):
    from tisma_code import agent
    s = agent.Session.__new__(agent.Session)
    s.pensar = None
    s.current = type("C", (), {"model": "qwen2.5-coder-1.5b"})()
    s.esfuerzo = "alto"
    assert s.pensar_activo() is True and s.nivel()["pasos"] == 80
    s.esfuerzo = "bajo"
    assert s.pensar_activo() is False and s.nivel()["rondas"] == 1
    s.esfuerzo = "medio"
    assert s.pensar_activo() is False                                     # automático: modelo chico, sin pensar
    s.pensar = True
    assert s.pensar_activo() is True                                      # lo elegido en «Pensar» manda


def test_progreso_de_reducir_llega_al_final(tmp_path, monkeypatch):
    """La barra avanza por etapas y termina en 100 % (antes solo se veía «Midiendo…» mucho rato)."""
    from tisma_code import models, reducir as red
    import tisma_code.motor as motor
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path)
    src = tmp_path / "m" / "x-f16.gguf"
    src.parent.mkdir()
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(motor, "gguf_info", lambda p: {"capas": 0, "tipo_archivo": 1})
    monkeypatch.setattr(red, "textos_de_muestra", lambda d: (d / "c.txt", d / "e.txt"))

    def run(argv, trabajo):
        if argv[0] == "llama-quantize":
            Path(argv[-3]).write_bytes(b"y" * 40)
        return "Final estimate: PPL = 10.0"
    from pathlib import Path
    pasos = []
    r = red.reducir(str(src), "auto", run=run, progreso=lambda i, n, t: pasos.append((i, n)))
    assert pasos[-1][0] == pasos[-1][1] and [i for i, _ in pasos] == sorted(i for i, _ in pasos)
    assert Path(r["archivo"]).exists()


def test_tamano_legible():
    from tisma_code.server import tamano_legible
    assert tamano_legible(612_000_000) == "612 MB" and tamano_legible(1_840_000_000) == "1,84 GB"


def test_avance_dentro_de_cada_etapa():
    from tisma_code.reducir import fraccion
    assert fraccion(["llama-imatrix", "--chunks", "24"], "computing...[1]5.1,[2]4.9,[6]4.7,") == 0.25
    assert fraccion(["llama-quantize", "a", "b"], "[  10/ 200] blk.0\n[ 100/ 200] blk.9") == 0.5
    assert fraccion(["llama-perplexity", "--chunks", "8"], "cargando") is None


def test_buscar_por_calidad_elige_el_mas_chico_que_llega():
    calidad = {"Q8_0": 99.5, "Q6_K": 99, "Q5_K_M": 98, "Q4_K_M": 95, "IQ4_XS": 93, "Q3_K_M": 89, "IQ3_M": 86,
               "IQ3_XXS": 80, "IQ2_M": 70, "Q2_K": 65}
    tipos = list(calidad)
    medidos = []
    elegido, m = r.buscar_por_calidad(tipos, lambda t: medidos.append(t) or calidad[t], 90)
    assert elegido == "IQ4_XS" and len(medidos) <= 4                     # 4 mediciones en vez de 10
    assert r.buscar_por_calidad(tipos, lambda t: calidad[t], 99.9)[0] is None   # ninguno llega
    assert r.buscar_por_calidad(tipos, lambda t: calidad[t], 50)[0] == "Q2_K"


def test_reducir_con_calidad_minima_de_punta_a_punta(tmp_path, monkeypatch):
    """«Acepto conservar al menos 90 %»: deja solo el formato más chico que lo cumple (medido)."""
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path / "models")
    d = tmp_path / "models" / "qwen"
    d.mkdir(parents=True)
    src = d / "qwen-1.5b-f16.gguf"
    src.write_bytes(b"x" * 1000)
    monkeypatch.setattr("tisma_code.motor.gguf_info", lambda p: {"capas": 28, "tipo_archivo": 1})
    calidad = {"Q8_0": 99.5, "Q6_K": 99, "Q5_K_M": 98, "Q4_K_M": 95, "IQ4_XS": 93, "Q3_K_M": 89, "IQ3_M": 86,
               "IQ3_XXS": 80, "IQ2_M": 70, "Q2_K": 65}
    hechos = {}

    def run(argv, trabajo):
        if argv[0] == "llama-quantize":
            hechos[argv[-3]] = argv[-2]
            Path(argv[-3]).write_bytes(b"y" * 100)
            return ""
        if argv[0] == "llama-perplexity":
            t = hechos.get(argv[2])
            return f"Final estimate: PPL = {10.0 if t is None else 10.0 * 100 / calidad[t]:.4f}"
        return ""
    res = r.reducir(str(src), "calidad:90", run=run, trabajo_base=tmp_path / "trabajo")
    assert res["tipo"] == "IQ4_XS" and 90 <= res["calidad"] < 95 and res["objetivo"] == 90
    assert Path(res["archivo"]).name == "qwen-1.5b-f16-reducido-iq4_xs.gguf" and src.exists()
    assert list((tmp_path / "trabajo").iterdir()) == []                    # las pruebas no quedan
    res = r.reducir(str(src), "calidad:99", run=run, trabajo_base=tmp_path / "trabajo")
    assert res["tipo"] == "Q6_K"
    with pytest.raises(r.ReducirError):
        r.reducir(str(src), "calidad:30", run=run, trabajo_base=tmp_path / "trabajo")
