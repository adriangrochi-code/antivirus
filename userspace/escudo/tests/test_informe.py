"""Informe claro de Escudo: frases simples, agrupadas, y privado para cada usuario."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from escudo import detonar, informe  # noqa: E402

SHA = "a" * 64


def test_traza_real_a_frases_simples():
    traza = ("100 execve(\"/tmp/m\", [\"/tmp/m\"], 0x0) = 0\n"
             "100 openat(AT_FDCWD, \"/home/usuario/.config/chromium/Default/Login Data\", O_RDONLY) = 3\n"
             "100 openat(AT_FDCWD, \"/home/usuario/.config/autostart/x.desktop\", O_WRONLY|O_CREAT, 0644) = 4\n"
             "100 connect(5, {sa_family=AF_INET, sin_port=htons(3333), sin_addr=inet_addr(\"1.2.3.4\")}, 16) = 0\n")
    inf = detonar.analizar_traza(traza, muestra="/tmp/m")
    inf.sha256 = SHA
    e = informe.explicar(inf.to_dict(), "regalo.exe")
    assert e["veredicto"] == "malicioso" and e["titulo"] == "regalo.exe: Peligroso"
    puntos = {g["titulo"]: g["puntos"] for g in e["grupos"]}
    assert "Intentó robar las contraseñas o sesiones guardadas del navegador." in puntos["Tus datos y contraseñas"]
    assert any("arrancar solo" in p for p in puntos["Arranque automático"])
    assert any("minería" in p for p in puntos["Internet"])
    assert any("1.2.3.4:3333" in t for t in e["tecnico"])          # el detalle técnico sigue disponible
    texto = json.dumps({k: v for k, v in e.items() if k != "tecnico"}, ensure_ascii=False)
    assert "señuelo" not in texto and "/home/" not in texto


def test_sin_nombrar_marcas():
    e = informe.explicar({"veredicto": "sospechoso", "motivos": [
        "intentó arrancar solo con Windows (registro «Run»): x", "se copió en la carpeta Inicio de Windows (arranque automático)"]})
    assert "Windows" not in json.dumps(e["grupos"], ensure_ascii=False)


def test_firma_conocida_y_limpio():
    e = informe.explicar({"veredicto": "malicioso", "motivos": ["detectado: Eicar-Test"]})
    assert e["grupos"][0]["titulo"] == "Virus conocido" and "Eicar-Test" in e["grupos"][0]["puntos"][0]
    assert informe.explicar({"veredicto": "limpio", "conexiones": ["8.8.8.8:443"]})["grupos"] == []


def test_privado_por_usuario(tmp_path):
    informe.guardar({"veredicto": "limpio", "sha256": SHA}, "/home/ana/Descargas/juego.exe", 1000, base=tmp_path)
    assert oct(os.stat(tmp_path / "1000").st_mode & 0o777) == "0o700"
    assert oct(os.stat(tmp_path / "1000" / f"{SHA}.json").st_mode & 0o777) == "0o600"
    assert informe.leer(1000, base=tmp_path)[0]["nombre"] == "juego.exe"     # sin la ruta completa
    assert informe.leer(1001, base=tmp_path) == []
    assert informe.guardar({"veredicto": "limpio", "sha256": "../x"}, "a", 1000, base=tmp_path) is None
    assert informe.guardar({"veredicto": "limpio", "sha256": SHA}, "a", 0, base=tmp_path) is None


def test_conserva_los_ultimos(tmp_path, monkeypatch):
    monkeypatch.setattr(informe, "MAX_POR_USUARIO", 3)
    for i in range(5):
        informe.guardar({"veredicto": "limpio", "sha256": f"{i:064x}"}, f"p{i}", 1000, base=tmp_path)
        os.utime(tmp_path / "1000" / f"{i:064x}.json", (i, i))
    assert len(list((tmp_path / "1000").glob("*.json"))) == 3


def test_orden_por_fecha_del_analisis(tmp_path):
    informe.guardar({"veredicto": "limpio", "sha256": "1" * 64}, "nuevo", 1000, base=tmp_path, ahora=lambda: 200)
    informe.guardar({"veredicto": "limpio", "sha256": "2" * 64}, "viejo", 1000, base=tmp_path, ahora=lambda: 100)
    assert [i["nombre"] for i in informe.leer(1000, base=tmp_path)] == ["nuevo", "viejo"]
