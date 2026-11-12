"""Las apps de TISMA con interfaz web se abren en su ventana (no como pestaña del navegador)."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from centinela import ventana  # noqa: E402


class Proc:
    def __init__(self, argv, **kw):
        self.argv, self.listo = argv, threading.Event()

    def wait(self):
        self.listo.wait(5)


def test_ventana_propia_y_cierre(tmp_path, monkeypatch):
    monkeypatch.setattr(ventana.shutil, "which", lambda n: "/usr/bin/chromium" if n == "chromium" else
                        ("/usr/bin/Xwayland" if n == "Xwayland" else None))
    procs, cerrada = [], threading.Event()

    def popen(argv, **kw):
        procs.append(Proc(argv))
        return procs[-1]
    ok = ventana.abrir_ventana("http://127.0.0.1:5/?t=x", "tisma-code", None, cerrada.set, popen=popen,
                               entorno={"DISPLAY": ":0", "XDG_RUNTIME_DIR": str(tmp_path)})
    argv = procs[0].argv
    assert ok and "--app=http://127.0.0.1:5/?t=x" in argv and "--class=tisma-code" in argv
    assert "--ozone-platform=x11" in argv and "--start-maximized" in argv          # icono propio en la barra
    perfil = next(a.split("=", 1)[1] for a in argv if a.startswith("--user-data-dir="))
    assert perfil.startswith(str(tmp_path)) and Path(perfil).is_dir()
    procs[0].listo.set()                                   # el usuario cierra la ventana
    assert cerrada.wait(5) and not Path(perfil).exists()   # la app termina y el perfil temporal se borra


def test_sin_chromium_usa_el_navegador(monkeypatch):
    monkeypatch.setattr(ventana.shutil, "which", lambda n: None)
    abiertas = []
    monkeypatch.setattr(ventana.webbrowser, "open", abiertas.append)
    assert ventana.abrir_ventana("http://x", "tisma-code") is False


def test_lanzadores_asociados_a_su_ventana():
    raiz = Path(__file__).resolve().parents[3] / "rootfs/usr/share/applications"
    for app, clase in (("Code", "tisma-code"), ("Rendimiento", "tisma-rendimiento")):
        assert f"StartupWMClass={clase}" in (raiz / f"net.tisma.{app}.desktop").read_text(encoding="utf-8")
    for srv in ("userspace/tisma-code/tisma_code/server.py", "userspace/rendimiento/tisma_rendimiento/server.py"):
        texto = (Path(__file__).resolve().parents[3] / srv).read_text(encoding="utf-8")
        assert "abrir_ventana(" in texto and "target=webbrowser.open, args=(url,)" in texto.split("def abrir(")[1]


def test_navegador_sin_marcadores_de_debian():
    import json
    raiz = Path(__file__).resolve().parents[3] / "rootfs"
    mp = json.loads((raiz / "etc/chromium/master_preferences").read_text(encoding="utf-8"))
    archivo = mp["distribution"]["import_bookmarks_from_file"]
    assert archivo == "/usr/share/tisma/chromium/marcadores.html"
    assert "debian" not in (raiz / archivo.lstrip("/")).read_text(encoding="utf-8").split("-->", 1)[1].lower()
    assert mp["bookmark_bar"]["show_on_all_tabs"] is False
