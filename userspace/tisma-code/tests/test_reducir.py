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
