"""Modo sin apuro: tareas largas con un modelo más inteligente del que entra en la memoria.

La idea de AirLLM (usar modelos más grandes que la memoria trayendo del disco lo que falta), pero con llama.cpp y
sin dependencias nuevas: llama-server lee el GGUF con mmap, y lo que no entra en la RAM se vuelve a leer del disco
en cada palabra. Es lento (minutos u horas), así que:
* Las tareas van a una cola (archivo del usuario) y las hace un proceso aparte (`tisma-code sin-apuro`): siguen
  aunque se cierre la ventana de TISMA Code.
* Solo trabaja enchufado: a batería el motor se congela (SIGSTOP) y sigue al volver la corriente.
* Con la prioridad mínima de CPU y de disco: lo que la persona esté usando va siempre primero.
* El motor, en la misma jaula que el de siempre (sin carpeta personal, sin capacidades). Nada sale del equipo.
* Al terminar avisa en el escritorio; el resultado queda guardado y se ve desde TISMA Code.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import models

DIR = models.MODELS_DIR.parent / "sin-apuro"
COLA = DIR / "tareas.json"
CERROJO = DIR / ".trabajando"
MAX_TOKENS = 3000
MAX_ADJUNTO = 200_000                        # caracteres de un archivo de texto adjunto
DISCO_MBS = 400                              # SSD SATA típico si no se midió; NVMe es más rápido, HDD mucho menos
PALABRAS_POR_TOKEN = 0.7
_lock = threading.Lock()


# ---------------------------------------------------------------- estimación
def disco_mbs(path: str) -> int:
    """MB/s aproximados del disco donde está el modelo: disco de platos ~100, SATA ~400, NVMe ~1500."""
    try:
        dev = os.stat(path).st_dev
        real = Path(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}").resolve()
    except OSError:
        return DISCO_MBS
    for d in [real, *real.parents]:              # partición → disco; con LUKS/LVM se sigue el primer «slave»
        esclavos = list((d / "slaves").glob("*")) if (d / "slaves").is_dir() else []
        if esclavos:
            return disco_mbs_de(esclavos[0].resolve())
        if (d / "queue/rotational").is_file():
            return disco_mbs_de(d)
    return DISCO_MBS


def disco_mbs_de(d: Path) -> int:
    for x in [d, *d.parents]:
        r = x / "queue/rotational"
        if r.is_file():
            if r.read_text().strip() == "1":
                return 100
            return 1500 if x.name.startswith("nvme") else DISCO_MBS
        if (x / "slaves").is_dir() and list((x / "slaves").glob("*")):
            return disco_mbs_de(next((x / "slaves").glob("*")).resolve())
    return DISCO_MBS


def estimar(path: str, hw: dict, palabras: int = 600) -> dict:
    """Velocidad esperada. Si el modelo no entra en la memoria libre, en cada palabra hay que releer del disco la
    parte que falta: manda la velocidad del disco (como en AirLLM)."""
    from . import motor
    tam = motor.total_size(path)
    libre = max(0.0, (hw.get("ram_mb", 0) * 0.92 - motor.SO_MB) * (1 << 20))
    bw = (hw.get("ram_bandwidth_gbs") or 20.0) * 1e9 * 0.6
    disco = (hw.get("disco_mbs") or disco_mbs(path)) * 1e6
    falta = max(0.0, tam - libre)
    seg_token = (tam - falta) / bw + falta / disco
    tok_s = 1 / seg_token if seg_token > 0 else 0.0
    tokens = palabras / PALABRAS_POR_TOKEN
    return {"cabe": falta == 0, "tamano": tam, "tokens_s": round(tok_s, 3),
            "minutos": round(tokens / tok_s / 60) if tok_s else None,
            "seg_por_palabra": round(1 / (tok_s * PALABRAS_POR_TOKEN), 1) if tok_s else None}


def texto_estimacion(e: dict) -> str:
    m = e.get("minutos")
    if m is None:
        return "No se pudo estimar"
    dur = f"unos {m} min" if m < 90 else f"unas {round(m / 60)} h"
    return ("Entra en la memoria" if e["cabe"] else "No entra en la memoria: lee del disco") + \
        f" · {dur} para una respuesta de ~600 palabras" + \
        (" — demasiado lento para este equipo: mejor un modelo más chico" if m > 24 * 60 else "")


# ---------------------------------------------------------------- cola
def _leer() -> list[dict]:
    try:
        d = json.loads(COLA.read_text())
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _escribir(tareas: list[dict]) -> None:
    DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = COLA.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tareas, f, ensure_ascii=False)
    os.replace(tmp, COLA)


def _cambiar(id_: str, **cambios) -> dict | None:
    with _lock, _cerrojo_cola():
        tareas = _leer()
        for t in tareas:
            if t["id"] == id_:
                t.update(cambios)
                _escribir(tareas)
                return dict(t)
    return None


class _cerrojo_cola:
    """Entre la app y el trabajador (dos procesos) también: flock sobre un archivo aparte."""
    def __enter__(self):
        DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(DIR / ".cola", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def agregar(pedido: str, modelo: str, adjunto: str = "", nombre_adjunto: str = "") -> dict:
    p = Path(modelo).resolve()
    if models.MODELS_DIR.resolve() not in p.parents or p.suffix != ".gguf" or not p.is_file():
        raise ValueError("Elige un modelo descargado por TISMA Code")
    pedido = pedido.strip()
    if not pedido:
        raise ValueError("Escribe qué quieres que haga")
    t = {"id": secrets.token_hex(6), "pedido": pedido[:20_000], "modelo": str(p), "adjunto": adjunto[:MAX_ADJUNTO],
         "nombre_adjunto": nombre_adjunto[:200], "estado": "en_cola", "creada": time.time(), "palabras": 0,
         "detalle": ""}
    with _lock, _cerrojo_cola():
        tareas = _leer()
        tareas.append(t)
        _escribir(tareas[-50:])
    return t


def listar() -> list[dict]:
    out = []
    for t in _leer():
        t = {k: v for k, v in t.items() if k != "adjunto"}
        out.append(t)
    return out[::-1]


def resultado(id_: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{12}", id_):
        raise ValueError("tarea desconocida")
    try:
        return (DIR / f"{id_}.md").read_text()
    except OSError:
        return ""


def cancelar(id_: str) -> bool:
    return _cambiar(id_, estado="cancelada") is not None


def borrar(id_: str) -> bool:
    with _lock, _cerrojo_cola():
        tareas = _leer()
        quedan = [t for t in tareas if t["id"] != id_ or t["estado"] in ("en_cola", "trabajando", "pausada")]
        if len(quedan) == len(tareas):
            return False
        _escribir(quedan)
    (DIR / f"{id_}.md").unlink(missing_ok=True)
    return True


def lanzar_trabajador(exe: str = "/usr/bin/tisma-code") -> None:
    """Arranca el trabajador si no hay uno (el cerrojo evita dos a la vez). Sigue al cerrar la ventana."""
    if not os.path.exists(exe):
        return
    subprocess.Popen([exe, "sin-apuro"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


# ---------------------------------------------------------------- trabajador
def enchufado() -> bool:
    from .server import enchufado as e
    return e()


def descendientes(pid: int) -> list[int]:
    out, pendientes = [], [pid]
    while pendientes:
        p = pendientes.pop()
        out.append(p)
        try:
            for t in os.listdir(f"/proc/{p}/task"):
                hijos = Path(f"/proc/{p}/task/{t}/children").read_text().split()
                pendientes += [int(h) for h in hijos]
        except OSError:
            continue
    return out


def senal(pid: int, sig: int) -> None:
    for p in descendientes(pid):
        try:
            os.kill(p, sig)
        except OSError:
            pass


class MotorLento(models.LocalServer):
    """El mismo motor enjaulado, con la prioridad más baja de CPU y de disco."""

    @staticmethod
    def _spawn(argv: list[str]) -> subprocess.Popen:
        pre = ["nice", "-n", "19"] + (["ionice", "-c", "3"] if shutil.which("ionice") else [])
        return subprocess.Popen(pre + models.jail_engine(argv), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)


def argumentos(path: str, hw: dict, ctx: int) -> list[str]:
    """Si entra en la memoria, lo que el motor TISMA midió o planeó; si no, CPU sin --mlock (el disco hace de
    memoria: fijarlo en RAM sería imposible)."""
    from . import motor
    if estimar(path, hw)["cabe"]:
        return [a for a in motor.config_for(path, hw, ctx)["args"]]
    return ["-ngl", "0", "--device", "none"]


def contexto_para(tarea: dict) -> int:
    tokens = (len(tarea["pedido"]) + len(tarea.get("adjunto", ""))) // 3 + MAX_TOKENS + 512
    return min(32768, max(4096, -(-tokens // 1024) * 1024))


def mensajes(tarea: dict) -> list[dict]:
    texto = tarea["pedido"]
    if tarea.get("adjunto"):
        texto += f"\n\n--- {tarea.get('nombre_adjunto') or 'archivo'} ---\n{tarea['adjunto']}"
    return [{"role": "system", "content": "Eres un asistente cuidadoso. Responde en el idioma de la persona, "
             "completo y bien ordenado. Tienes tiempo: revisa lo que escribes."},
            {"role": "user", "content": texto}]


def generar(url: str, clave: str, tarea: dict, pensar: bool, al_avanzar) -> str:
    cuerpo = {"model": "local", "messages": mensajes(tarea), "stream": True, "max_tokens": MAX_TOKENS,
              "temperature": 0.3}
    if not pensar:
        cuerpo["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(url + "/chat/completions", data=json.dumps(cuerpo).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {clave}"})
    texto = []
    with urllib.request.urlopen(req, timeout=None) as r:     # noqa: S310 (127.0.0.1, motor propio)
        for linea in r:
            linea = linea.strip()
            if not linea.startswith(b"data:"):
                continue
            dato = linea[5:].strip()
            if dato == b"[DONE]":
                break
            try:
                d = json.loads(dato)["choices"][0].get("delta", {}).get("content") or ""
            except (ValueError, KeyError, IndexError):
                continue
            if d:
                texto.append(d)
                al_avanzar("".join(texto))
    return "".join(texto)


def ejecutar(tarea: dict, hw: dict, pensar: bool = False) -> str:
    srv = MotorLento()
    path, ctx = tarea["modelo"], contexto_para(tarea)
    salida = DIR / f"{tarea['id']}.md"
    estado = {"pausado": False, "fin": False}

    def vigilar():
        """Congela el motor a batería y lo despierta con corriente; lo detiene si se cancela la tarea."""
        while not estado["fin"]:
            time.sleep(20)
            if estado["fin"] or not srv.proc:
                continue
            actual = next((t for t in _leer() if t["id"] == tarea["id"]), {})
            if actual.get("estado") == "cancelada":
                senal(srv.proc.pid, signal.SIGCONT)
                srv.stop()
                return
            quiere_pausa = not enchufado()
            if quiere_pausa != estado["pausado"]:
                senal(srv.proc.pid, signal.SIGSTOP if quiere_pausa else signal.SIGCONT)
                estado["pausado"] = quiere_pausa
                _cambiar(tarea["id"], estado="pausada" if quiere_pausa else "trabajando",
                         detalle="En pausa: conecta el cargador para seguir" if quiere_pausa else "")

    ultimo = [0.0]

    def al_avanzar(texto: str) -> None:
        if time.monotonic() - ultimo[0] < 15:
            return
        ultimo[0] = time.monotonic()
        salida.write_text(texto)
        _cambiar(tarea["id"], palabras=len(texto.split()))

    try:
        url = srv.start(path, hw, ctx=ctx, extra=argumentos(path, hw, ctx))
        fin_espera = time.monotonic() + 900
        while not models.port_listening(srv.port):
            if time.monotonic() > fin_espera or srv.proc.poll() is not None:
                raise RuntimeError("el motor no arrancó con este modelo")
            time.sleep(2)
        time.sleep(2)
        threading.Thread(target=vigilar, daemon=True).start()
        texto = generar(url, srv.api_key, tarea, pensar, al_avanzar)
        salida.write_text(texto)
        return texto
    finally:
        estado["fin"] = True
        srv.stop()


def avisar(titulo: str, texto: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "TISMA Code", "-i", "tisma-code", titulo, texto], check=False,
                       timeout=10)


def siguiente() -> dict | None:
    for t in _leer():
        if t["estado"] in ("en_cola", "trabajando", "pausada"):
            return t
    return None


def trabajar(hw: dict | None = None, pensar: bool = False, espera=time.sleep) -> int:
    """Hace la cola entera, una tarea a la vez; termina cuando no queda nada. Un solo trabajador a la vez."""
    DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(CERROJO, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return 0                                  # ya hay uno trabajando
    hechas = 0
    try:
        hw = hw or models.hardware()
        while (t := siguiente()):
            if not enchufado():
                _cambiar(t["id"], estado="pausada", detalle="En pausa: conecta el cargador para empezar")
                espera(60)
                continue
            _cambiar(t["id"], estado="trabajando", detalle="", empezada=time.time())
            try:
                texto = ejecutar(t, hw, pensar)
            except (OSError, RuntimeError, ValueError, urllib.error.URLError) as e:
                actual = next((x for x in _leer() if x["id"] == t["id"]), {})
                if actual.get("estado") != "cancelada":
                    _cambiar(t["id"], estado="error", detalle=f"No se pudo terminar: {e}"[:300])
                continue
            actual = next((x for x in _leer() if x["id"] == t["id"]), {})
            if actual.get("estado") == "cancelada":
                continue
            _cambiar(t["id"], estado="lista", terminada=time.time(), palabras=len(texto.split()), detalle="")
            hechas += 1
            avisar("Tu tarea sin apuro está lista", t["pedido"][:120] + " — ábrela en TISMA Code")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return hechas


def main() -> int:
    try:
        cfg = json.loads((Path.home() / ".config/tisma-code/config.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    trabajar(pensar=cfg.get("pensar") == "si")
    return 0
