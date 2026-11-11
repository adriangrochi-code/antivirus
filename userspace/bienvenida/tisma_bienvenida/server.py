"""Bienvenida TISMA: el primer inicio en cinco pasos (aspecto, uso del equipo, IA local, protección, listo).

Servidor local (127.0.0.1, token aleatorio, Host verificado) que solo ejecuta órdenes FIJAS con argumentos
de listas cerradas: nada de lo que llega por HTTP se pasa a un intérprete. Lo aplicado es del usuario (tema,
lanzadores del panel) salvo el perfil de potencia, que ya autoriza polkit para el usuario de la sesión.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

for p in ("/usr/lib/centinela/python", str(Path(__file__).resolve().parents[2] / "tisma-code")):
    if p not in sys.path:
        sys.path.append(p)

STATIC = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)       # de la API: solo viaja dentro de la página y en una cookie HttpOnly
BOOT = secrets.token_urlsafe(24)        # de la URL inicial: un solo uso (la URL queda en el argv de Chromium,
BOOT_USED = threading.Event()           # que cualquier usuario local puede leer en /proc)
RUN = subprocess.run              # puntos de sustitución en las pruebas
POPEN = subprocess.Popen
APPS_DIR = Path(os.environ.get("TISMA_APPS_DIR", "/usr/share/applications"))
CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tisma" / "bienvenida.json"

ACENTOS = {"violeta": "#9145E6", "azul": "#3D8BFD", "verde": "#2FB37A", "naranja": "#F08A24", "rosa": "#E0457B",
           "grafito": "#6E6C78"}
TEMAS = {"oscuro": ("CentinelaAero", "Papirus-Dark"), "claro": ("TismaClaro", "Papirus-Light")}

# Lanzadores: para cada app, nombres posibles del .desktop (cambian entre versiones de Debian)
APP = {
    "archivos": ["org.kde.dolphin"], "navegador": ["net.tisma.Navegador", "chromium", "firefox-esr"],
    "correo": ["thunderbird", "org.mozilla.Thunderbird"], "oficina": ["libreoffice-startcenter"],
    "texto": ["libreoffice-writer"], "pdf": ["org.kde.okular"], "musica": ["org.kde.elisa"],
    "fotos": ["org.kde.gwenview"], "tienda": ["tisma-tienda", "org.kde.discover"], "code": ["net.tisma.Code"],
    "strata": ["strata"], "terminal": ["org.kde.konsole"], "kate": ["org.kde.kate"],
    "laminador": ["net.tisma.Tisma"], "freecad": ["org.freecad.FreeCAD", "freecad"], "kicad": ["org.kicad.kicad", "kicad"], "openscad": ["openscad"],
    "inkscape": ["org.inkscape.Inkscape", "inkscape"], "blender": ["blender"], "gimp": ["org.gimp.GIMP", "gimp"],
    "steam": ["steam"], "rendimiento": ["net.tisma.Rendimiento"], "kdenlive": ["org.kde.kdenlive"],
    "obs": ["com.obsproject.Studio", "obs"],
}
PERFILES = {
    "diario": {"nombre": "Uso diario", "desc": "Navegar, correo, documentos, fotos y música",
               "apps": ["archivos", "navegador", "correo", "oficina", "musica", "tienda"], "potencia": "equilibrado",
               "listas": ["diario"]},
    "estudio": {"nombre": "Estudio y oficina", "desc": "Documentos, PDF, presentaciones y la IA para estudiar",
                "apps": ["archivos", "navegador", "texto", "pdf", "code", "tienda"], "potencia": "equilibrado",
                "listas": ["estudio"]},
    "ingenieria": {"nombre": "Ingeniería y diseño", "desc": "CAD, electrónica, simulación, diseño gráfico y 3D",
                   "apps": ["archivos", "navegador", "freecad", "kicad", "openscad", "laminador", "blender", "code"],
                   "potencia": "rendimiento", "listas": ["ingenieria"]},
    "programacion": {"nombre": "Programación", "desc": "Terminal, editor y agentes de IA locales",
                     "apps": ["archivos", "navegador", "terminal", "kate", "code", "strata"],
                     "potencia": "equilibrado", "listas": ["programacion"]},
    "juegos": {"nombre": "Juegos y creación", "desc": "Steam, grabación y edición de vídeo, rendimiento",
               "apps": ["archivos", "navegador", "steam", "obs", "kdenlive", "rendimiento"], "potencia": "rendimiento",
               "listas": ["juegos"]},
}
ABRIR = {"code": ["tisma-code"], "tienda": ["plasma-discover"], "rendimiento": ["tisma-rendimiento"],
         "ajustes": ["systemsettings"]}
# Las apps de cada perfil no van en la ISO (pesaría ~6 GB): se instalan aquí con tisma-perfil (pkexec)
PERFIL_ESTADO = Path(os.environ.get("TISMA_PERFIL_ESTADO", "/run/tisma-perfil/estado.json"))
INSTALANDO: dict = {}             # {"proc": Popen, "perfil": str, "inicio": float}
SERVICIOS = {"escudo": "Antivirus y cortafuegos (Escudo)", "nftables": "Cortafuegos del sistema",
             "usbguard": "Bloqueo de USB desconocidos", "tisma-copias-instantanea.timer": "Instantáneas diarias del sistema",
             "dnscrypt-proxy": "DNS cifrado y sin rastreadores", "apparmor": "Aislamiento de programas (AppArmor)"}


def read_conf() -> dict:
    try:
        return json.loads(CONF.read_text())
    except (OSError, ValueError):
        return {}


def write_conf(**kv) -> None:
    d = {**read_conf(), **kv}
    CONF.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    tmp.replace(CONF)


def desktop_ids(keys: list[str]) -> list[str]:
    out = []
    for k in keys:
        for cand in APP.get(k, []):
            if (APPS_DIR / f"{cand}.desktop").exists():
                out.append(f"applications:{cand}.desktop")
                break
    return out


def panel_script(launchers: list[str]) -> str:
    """JavaScript de Plasma que cambia los iconos anclados de la barra de tareas (solo datos de la lista fija)."""
    return ("var ps = panels(); for (var i = 0; i < ps.length; i++) { var ws = ps[i].widgets('org.kde.plasma.icontasks');"
            " for (var j = 0; j < ws.length; j++) { ws[j].currentConfigGroup = ['General'];"
            f" ws[j].writeConfig('launchers', {json.dumps(launchers)}); ws[j].reloadConfig(); }} }}")


def _ok(argv: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        r = RUN(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stderr or r.stdout or "")[-300:]
    except FileNotFoundError:
        return False, f"falta «{argv[0]}» (¿no es una sesión de Plasma?)"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def changeicons() -> str | None:
    for d in ("/usr/lib/x86_64-linux-gnu/libexec", "/usr/libexec", "/usr/lib/libexec"):
        p = Path(d) / "plasma-changeicons"
        if p.exists():
            return str(p)
    return None


def current_scheme() -> str:
    for f in (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kdeglobals", Path("/etc/xdg/kdeglobals")):
        try:
            for line in f.read_text().splitlines():
                if line.startswith("ColorScheme="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return ""


def apply_theme(modo: str, acento: str) -> dict:
    scheme, icons = TEMAS[modo]
    ok, out = True, ""
    # El esquema solo si cambia: si ya está puesto, plasma-apply-colorscheme puede terminar con error y no
    # aplicar el acento (el caso más común: seguir en oscuro y cambiar solo el color)
    if current_scheme() != scheme:
        ok, out = _ok(["plasma-apply-colorscheme", scheme])
    ok2, out2 = _ok(["plasma-apply-colorscheme", "--accent-color", ACENTOS[acento]])
    ok, out = ok and ok2, out or out2
    ci = changeicons()
    if ci:
        _ok([ci, icons])
    write_conf(tema=modo, acento=acento)
    return {"ok": ok, "salida": out}


def apply_profile(perfil: str) -> dict:
    p = PERFILES[perfil]
    launchers = desktop_ids(p["apps"])
    ok1, out1 = _ok(["gdbus", "call", "--session", "--dest", "org.kde.plasmashell", "--object-path", "/PlasmaShell",
                     "--method", "org.kde.PlasmaShell.evaluateScript", panel_script(launchers)])
    ok2, _ = _ok(["systemctl", "start", "--no-block", f"centinela-perfil@{p['potencia']}.service"])
    write_conf(perfil=perfil)
    return {"ok": ok1, "lanzadores": launchers, "potencia": ok2, "salida": out1}


def live_session() -> bool:
    """Sesión live (USB): lo instalado iría a la RAM y se perdería al apagar."""
    try:
        return Path("/run/live/medium").exists() or "boot=live" in Path("/proc/cmdline").read_text().split()
    except OSError:
        return False


def missing_for(perfil: str) -> dict:
    """Lo que falta de las apps del perfil (paquetes de Debian y componentes de GitHub)."""
    try:
        r = RUN(["tisma-perfil", "faltan", *PERFILES[perfil]["listas"]], capture_output=True, text=True, timeout=30)
        d = json.loads(r.stdout or "{}")
        return {"paquetes": len(d.get("paquetes", [])), "componentes": len(d.get("componentes", []))}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"paquetes": 0, "componentes": 0, "sin_herramienta": True}


def start_install(perfil: str) -> dict:
    proc = INSTALANDO.get("proc")
    if proc is not None and proc.poll() is None:
        return {"ok": False, "error": "Ya hay una instalación en curso"}
    if live_session():
        return {"ok": False, "error": "En la sesión live se perdería al apagar: instala TISMA OS en el disco y "
                                       "elige el perfil en el primer inicio"}
    INSTALANDO.update(perfil=perfil, inicio=time.time(), proc=POPEN(
        ["pkexec", "tisma-perfil", "instalar", *PERFILES[perfil]["listas"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
    return {"ok": True}


def install_state() -> dict:
    proc = INSTALANDO.get("proc")
    if proc is None:
        return {"fase": "ninguna"}
    try:
        st = json.loads(PERFIL_ESTADO.read_text())
        if st.get("inicio", 0) < INSTALANDO["inicio"] - 5:
            st = {}                                 # de una instalación anterior
    except (OSError, ValueError):
        st = {}
    rc = proc.poll()
    if rc is not None and not st.get("fase") in ("listo", "error"):
        # pkexec: 126 = no autorizado / ventana cerrada, 127 = no se pudo autenticar
        st = {"fase": "cancelado" if rc in (126, 127) else "error", "ok": False,
              "mensaje": "" if rc in (126, 127) else f"terminó con código {rc}"}
    return {"perfil": INSTALANDO["perfil"], "fase": st.get("fase", "esperando permiso"), "ok": st.get("ok"),
            "paquetes": st.get("paquetes"), "mensaje": (st.get("mensaje") or "")[-300:], "activo": rc is None}


def security_state() -> list[dict]:
    rows = []
    for unit, label in SERVICIOS.items():
        ok, out = _ok(["systemctl", "is-active", unit], timeout=10)
        rows.append({"id": unit, "nombre": label, "activo": ok and out.strip() in ("active", "")})
    return rows


def ai_state() -> dict:
    try:
        from tisma_code import models, motor
    except ImportError:
        return {"motor": "—", "motivo": "TISMA Code no está instalado", "consejo": "", "modelos": 0}
    hw = models.hardware()
    gb = hw.get("ram_mb", 0) / 1024
    eng = motor.choose_engine(hw, bool(models.endpoint_models("http://127.0.0.1:8080/v1")))
    rec = motor.choose_model(models.local_models(), hw)
    if eng["motor"] == "strata":
        consejo = "Tu equipo puede con Strata: modelos grandes repartidos entre la GPU, la RAM y el disco."
    elif gb < 9:
        consejo = "Con esta memoria, un modelo de 1 a 4 mil millones de parámetros (Q4) va fluido."
    elif gb < 20:
        consejo = ("Un modelo de 7–8 mil millones (Q4) o un MoE con ~3 mil millones activos que ocupe hasta "
                   "~11 GB: la mejor calidad que va fluida en este equipo.")
    else:
        consejo = "Un MoE de ~30 mil millones (~3 activos) en Q4 o un modelo denso de 14 mil millones."
    return {"motor": eng["motor"], "motivo": eng["motivo"], "consejo": consejo,
            "modelos": len(models.local_models()), "recomendado": Path(rec["path"]).name if rec else "",
            "ram_gb": round(gb), "gpu": ", ".join(g["name"] for g in hw.get("gpus", [])) or "integrada"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if getattr(self, "_cookie", None):
                self.send_header("Set-Cookie", self._cookie)
                self._cookie = None
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _auth(self):
        return self._host_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def _cookie_ok(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "bv" and secrets.compare_digest(v, TOKEN):
                return True
        return False

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        if u.path == "/":
            boot = secrets.compare_digest(q.get("t", ""), BOOT) and not BOOT_USED.is_set()
            if not (boot or self._cookie_ok()):
                return self._send(403, b"Abre la Bienvenida desde su lanzador.", "text/plain")
            BOOT_USED.set()                   # la URL del argv ya no sirve a nadie más
            self._cookie = f"bv={TOKEN}; HttpOnly; SameSite=Strict; Path=/"
            return self._send(200, (STATIC / "index.html").read_text().replace("__TOKEN__", TOKEN).encode(), "text/html")
        if u.path == "/static/logo.svg":
            return self._send(200, (STATIC / "logo.svg").read_bytes(), "image/svg+xml")
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        if u.path == "/api/estado":
            c = read_conf()
            return self._send(200, {"tema": c.get("tema", "oscuro"), "acento": c.get("acento", "violeta"),
                                    "perfil": c.get("perfil", ""), "acentos": ACENTOS,
                                    "perfiles": {k: {"nombre": v["nombre"], "desc": v["desc"]} for k, v in PERFILES.items()},
                                    "usuario": os.environ.get("USER", ""), "live": live_session()})
        if u.path == "/api/faltan":
            if q.get("perfil") not in PERFILES:
                return self._send(400, {"error": "perfil inválido"})
            return self._send(200, missing_for(q["perfil"]))
        if u.path == "/api/instalacion":
            return self._send(200, install_state())
        if u.path == "/api/ia":
            return self._send(200, ai_state())
        if u.path == "/api/seguridad":
            return self._send(200, {"servicios": security_state()})
        return self._send(404, {"error": "no encontrado"})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return self._send(403, {"error": "no autorizado"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            b = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "JSON inválido"})
        u = urlparse(self.path)
        if u.path == "/api/tema":
            if b.get("modo") not in TEMAS or b.get("acento") not in ACENTOS:
                return self._send(400, {"error": "tema o acento inválido"})
            return self._send(200, apply_theme(b["modo"], b["acento"]))
        if u.path == "/api/perfil":
            if b.get("perfil") not in PERFILES:
                return self._send(400, {"error": "perfil inválido"})
            return self._send(200, apply_profile(b["perfil"]))
        if u.path == "/api/instalar":
            if b.get("perfil") not in PERFILES:
                return self._send(400, {"error": "perfil inválido"})
            return self._send(200, start_install(b["perfil"]))
        if u.path == "/api/abrir":
            argv = ABRIR.get(b.get("app"))
            if not argv or not shutil.which(argv[0]):
                return self._send(400, {"error": "aplicación no disponible"})
            POPEN(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return self._send(200, {"ok": True})
        if u.path == "/api/listo":
            write_conf(hecha=True, fecha=time.strftime("%Y-%m-%d"))
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "no encontrado"})


def open_window(url: str):
    """Ventana propia (sin pestañas ni barra de direcciones) si hay Chromium; si no, el navegador."""
    exe = shutil.which("chromium") or shutil.which("chromium-browser")
    if exe:
        import tempfile
        # Perfil propio y temporal: con uno fijo, una segunda Bienvenida le pasaba su URL a la primera y se
        # cerraba al instante; basic: sin pedir crear un monedero de KWallet al primer inicio de sesión
        prof = tempfile.mkdtemp(prefix="tisma-bienvenida-", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
        return POPEN([exe, f"--app={url}", f"--user-data-dir={prof}", "--class=tisma-bienvenida", "--no-first-run",
                      "--password-store=basic", "--window-size=980,700", "--disable-features=Translate"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    webbrowser.open(url)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tisma-bienvenida")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--si-primera-vez", action="store_true", help="solo si no se completó antes (inicio de sesión)")
    a = ap.parse_args(argv)
    if a.si_primera_vez:
        c = read_conf()
        if c.get("hecha") or c.get("mostrada") or Path("/run/live").exists():
            return 0                                    # ya vista (aunque se cerrase con la X), o sesión live
        write_conf(mostrada=True)                       # sigue en el menú: «Bienvenida a TISMA»
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{httpd.server_port}/?t={BOOT}"
    print(f"Bienvenida TISMA en el puerto {httpd.server_port}", flush=True)   # la URL no va al registro
    if a.no_browser:
        print(url, flush=True)
    if not a.no_browser:
        win = open_window(url)
        if win is not None:                             # al cerrar la ventana, se cierra el servidor
            threading.Thread(target=lambda: (win.wait(), httpd.shutdown()), daemon=True).start()
    t = threading.Timer(4 * 3600, httpd.shutdown)       # nunca queda un servidor olvidado
    t.daemon = True
    t.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
