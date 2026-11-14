"""Preferencias: solo órdenes fijas con valores de listas cerradas."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_preferencias import server as s  # noqa: E402


class Run:
    def __init__(self, out=""):
        self.calls, self.out = [], out

    def __call__(self, argv, **k):
        self.calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": self.out, "stderr": ""})()


def test_tamano_escribe_todas_las_fuentes(monkeypatch, tmp_path):
    r = Run()
    monkeypatch.setattr(s, "RUN", r)
    monkeypatch.setattr(s, "CONFIG", tmp_path)
    assert s.poner_tamano("grande")["ok"]
    claves = {c[c.index("--key") + 1]: c[-1] for c in r.calls if "--key" in c}
    assert claves["font"].startswith("Inter,12,") and claves["fixed"].startswith("JetBrains Mono,12,")
    assert claves["smallestReadableFont"].startswith("Inter,11,")
    (tmp_path / "kdeglobals").write_text("[General]\nfont=Inter,14,-1\n")
    assert s.tamano_actual() == "muy_grande"


def test_fondos_y_miniaturas_sin_salir_de_la_carpeta(monkeypatch, tmp_path):
    d = tmp_path / "Montaña" / "contents" / "images"
    d.mkdir(parents=True)
    (d / "1920x1080.jpg").write_bytes(b"x")
    (tmp_path / "Vacio").mkdir()
    monkeypatch.setattr(s, "FONDOS", tmp_path)
    assert [f["id"] for f in s.fondos()] == ["Montaña"]
    assert s.miniatura("../etc") is None and s.miniatura("Vacio") is None


def test_foto_propia_valida_tipo(monkeypatch, tmp_path):
    foto = tmp_path / "gato.png"
    foto.write_bytes(b"x")
    r = Run(str(foto) + "\n")
    monkeypatch.setattr(s, "RUN", r)
    monkeypatch.setattr(s, "CONF", tmp_path / "p.json")
    monkeypatch.setattr(s.shutil, "which", lambda x: "/usr/bin/" + x)
    assert s.elegir_foto()["ok"]
    assert ["plasma-apply-wallpaperimage", str(foto)] in r.calls
    txt = tmp_path / "virus.sh"
    txt.write_text("x")
    monkeypatch.setattr(s, "RUN", Run(str(txt)))
    assert not s.elegir_foto()["ok"]


def test_pantalla_nunca_avisa_y_bateria_tope(monkeypatch, tmp_path):
    r = Run()
    monkeypatch.setattr(s, "RUN", r)
    monkeypatch.setattr(s, "CONF", tmp_path / "p.json")
    s.poner_pantalla(30)
    seg = {tuple(c[c.index("--group") + 1:c.index("--key"):2]): c[-1] for c in r.calls
           if "TurnOffDisplayIdleTimeoutSec" in c}
    assert seg[("AC", "Display")] == "1800" and seg[("Battery", "Display")] == "300"
    assert "puede ver" in s.poner_pantalla(0)["nota"]


def test_energia_solo_su_servicio(monkeypatch, tmp_path):
    r = Run()
    monkeypatch.setattr(s, "RUN", r)
    monkeypatch.setattr(s, "CONF", tmp_path / "p.json")
    s.poner_energia("silencioso")
    assert r.calls == [["systemctl", "start", "centinela-perfil@silencioso.service"]]
    assert json.loads((tmp_path / "p.json").read_text())["energia"] == "silencioso"
