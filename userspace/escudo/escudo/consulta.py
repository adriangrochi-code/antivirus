"""Consulta a Escudo: «¿puedo abrir este archivo?», para lo que el kernel no ejecuta.

La puerta de primera ejecución (primera_ejecucion.py) frena lo que el kernel EJECUTA. Un programa de Windows no lo
ejecuta el kernel: Wine lo lee como datos, así que la puerta no lo ve. El lanzador del doble clic
(tisma-abrir-windows) pregunta aquí antes de abrirlo.

Protocolo (socket Unix /run/escudo/consulta.sock, cualquiera puede conectarse):
- el cliente envía una línea JSON {"accion": "revisar"} y, en el mismo mensaje, el ARCHIVO ABIERTO (SCM_RIGHTS):
  así Escudo analiza exactamente esos bytes y nunca abre una ruta que el usuario pudiera cambiar por un enlace a un
  archivo ajeno (solo puede pedir que se revise lo que él mismo ya puede leer);
- Escudo copia el contenido a su carpeta privada, calcula el SHA-256 y responde líneas JSON:
  {"estado": "analizando", ...} mientras analiza y {"estado": "listo", "veredicto": "limpio"|"sospechoso"|
  "malicioso", "motivos": [...], "sha256": ...} al final (o {"estado": "error", "mensaje": ...}).
- Veredicto por huella: lo ya analizado (o permitido por un administrador) se responde al instante.
- El análisis se hace con la identidad de quien pregunta (nunca como root), uno por usuario a la vez y como mucho
  dos a la vez en total.
"""

from __future__ import annotations

import array
import hashlib
import json
import logging
import os
import socket
import stat
import struct
import tempfile
import threading
from pathlib import Path

from . import detonar

log = logging.getLogger("escudo.consulta")

SOCK = Path(os.environ.get("ESCUDO_CONSULTA", "/run/escudo/consulta.sock"))
MAX_BYTES = 512 << 20
MAX_PEDIDO = 4096


def credenciales(conn: socket.socket) -> tuple[int, int, int]:
    """(pid, uid, gid) de quien se conecta, según el kernel (no se puede falsificar)."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


def recibir(conn: socket.socket) -> tuple[dict, int | None]:
    """El pedido JSON y, si vino, el descriptor de archivo adjunto."""
    fds = array.array("i")
    msg, anc, _flags, _addr = conn.recvmsg(MAX_PEDIDO, socket.CMSG_LEN(4 * fds.itemsize))
    fd = None
    for nivel, tipo, datos in anc:
        if nivel == socket.SOL_SOCKET and tipo == socket.SCM_RIGHTS:
            fds.frombytes(datos[: len(datos) - (len(datos) % fds.itemsize)])
    if fds:
        fd = fds[0]
        for extra in fds[1:]:              # solo se acepta uno: el resto se cierra
            os.close(extra)
    try:
        pedido = json.loads(msg.decode("utf-8", "replace").strip() or "{}")
    except ValueError:
        pedido = {}
    return (pedido if isinstance(pedido, dict) else {}), fd


def copiar(fd: int, destino: Path) -> str:
    """Copia el archivo recibido a la carpeta privada de Escudo y devuelve su SHA-256. Solo archivos regulares."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("no es un archivo normal")
    if st.st_size > MAX_BYTES:
        raise ValueError("el archivo es demasiado grande para analizarlo (más de 512 MB)")
    h = hashlib.sha256()
    total = 0
    with os.fdopen(os.dup(fd), "rb") as src, open(destino, "xb") as dst:
        os.lseek(src.fileno(), 0, os.SEEK_SET)
        while chunk := src.read(1 << 20):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("el archivo creció durante la copia")
            h.update(chunk)
            dst.write(chunk)
    os.chmod(destino, 0o644)               # el análisis corre con la identidad del usuario: tiene que poder leerlo
    return h.hexdigest()


def enviar(conn: socket.socket, d: dict) -> None:
    try:
        conn.sendall((json.dumps(d, ensure_ascii=False) + "\n").encode())
    except OSError:
        pass


class Consulta:
    def __init__(self, analizador, sock: Path | None = None, det_dir: Path | None = None, cargar=None):
        """`analizador` tiene .analyze(ruta, sha, uid, gid) -> Informe (la puerta de primera ejecución)."""
        self.analizador = analizador
        self.sock_path = sock or SOCK
        from .primera_ejecucion import DET_DIR
        self.det_dir = det_dir or DET_DIR
        self.cargar = cargar or detonar.cargar_permitidos
        self._total = threading.BoundedSemaphore(2)
        self._por_usuario: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.srv: socket.socket | None = None

    def start(self) -> threading.Thread:
        self.det_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        srv.bind(str(self.sock_path))
        os.chmod(self.sock_path, 0o666)    # cualquier usuario puede pedir que se revise algo que ya puede leer
        srv.listen(16)
        self.srv = srv
        t = threading.Thread(target=self._aceptar, daemon=True, name="consulta")
        t.start()
        log.info("Consulta de archivos activa en %s", self.sock_path)
        return t

    def stop(self) -> None:
        self._stop.set()
        if self.srv:
            try:
                self.srv.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.srv.close()

    def _aceptar(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.atender, args=(conn,), daemon=True, name="consulta-pedido").start()

    def atender(self, conn: socket.socket) -> None:
        fd = None
        try:
            conn.settimeout(10)
            _pid, uid, gid = credenciales(conn)
            pedido, fd = recibir(conn)
            conn.settimeout(None)
            if pedido.get("accion") != "revisar" or fd is None:
                enviar(conn, {"estado": "error", "mensaje": "pedido no válido: hace falta el archivo abierto"})
                return
            self.revisar(conn, fd, uid, gid, str(pedido.get("nombre") or "programa"))
        except Exception as e:  # noqa: BLE001 (un pedido roto nunca tumba a Escudo)
            log.warning("Consulta fallida: %s", e)
            enviar(conn, {"estado": "error", "mensaje": "Escudo no pudo revisar el archivo"})
        finally:
            if fd is not None:
                os.close(fd)
            conn.close()

    def revisar(self, conn: socket.socket, fd: int, uid: int, gid: int, nombre: str) -> None:
        nombre = os.path.basename(nombre.replace("\x00", ""))[:120] or "programa"
        with self._lock:
            if uid in self._por_usuario:
                enviar(conn, {"estado": "error", "mensaje": "Ya hay otra revisión tuya en curso: espera a que termine"})
                return
            self._por_usuario.add(uid)
        carpeta = Path(tempfile.mkdtemp(prefix="consulta-", dir=self.det_dir))
        try:
            os.chmod(carpeta, 0o755)
            ruta = carpeta / nombre
            try:
                sha = copiar(fd, ruta)
            except (ValueError, OSError) as e:
                enviar(conn, {"estado": "error", "mensaje": str(e)})
                return
            previo = self.cargar().get(sha)
            if previo and (previo.get("permitido_por_usuario") or previo.get("veredicto") in
                           ("limpio", "sospechoso", "malicioso")):
                veredicto = "limpio" if previo.get("permitido_por_usuario") and previo.get("veredicto") != "malicioso" \
                    else previo["veredicto"]
                enviar(conn, {"estado": "listo", "veredicto": veredicto, "motivos": previo.get("motivos", []),
                              "sha256": sha, "ya_revisado": True})
                return
            enviar(conn, {"estado": "analizando", "sha256": sha})
            with self._total:
                inf = self.analizador.analyze(str(ruta), sha, uid if uid else 65534, gid if uid else 65534)
            if inf is None:
                enviar(conn, {"estado": "error", "mensaje": "el archivo cambió durante la revisión"})
                return
            enviar(conn, {"estado": "listo", "veredicto": inf.veredicto, "motivos": inf.motivos[:5], "sha256": sha,
                          "entorno": inf.entorno, "nota": inf.nota})
        finally:
            with self._lock:
                self._por_usuario.discard(uid)
            import shutil
            shutil.rmtree(carpeta, ignore_errors=True)


# ---------------------------------------------------------------- cliente (lo usa tisma-abrir-windows)
def pedir_revision(ruta: str, al_avanzar=None, sock: Path | None = None, timeout: float = 600) -> dict:
    """Envía el archivo abierto a Escudo y devuelve la respuesta final."""
    with open(ruta, "rb") as fh, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC) as s:
        s.settimeout(timeout)
        s.connect(str(sock or SOCK))
        datos = json.dumps({"accion": "revisar", "nombre": os.path.basename(ruta)}).encode() + b"\n"
        s.sendmsg([datos], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fh.fileno()]))])
        buf = b""
        while True:
            parte = s.recv(65536)
            if not parte:
                return {"estado": "error", "mensaje": "Escudo cerró la conexión"}
            buf += parte
            while b"\n" in buf:
                linea, buf = buf.split(b"\n", 1)
                try:
                    d = json.loads(linea)
                except ValueError:
                    continue
                if d.get("estado") == "analizando":
                    if al_avanzar:
                        al_avanzar(d)
                    continue
                return d
