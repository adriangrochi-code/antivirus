"""Puerta de primera ejecución: lo que viene de fuera no se ejecuta hasta haberlo visto en un entorno controlado.

Escudo (root) pide al kernel, con fanotify, que le consulte cada ejecución de un archivo en /home, /tmp, /var/tmp
y las memorias USB (/media, /run/media). Para cada una decide en microsegundos:

* NO viene de fuera (lo compilaste, lo instaló apt/pip/Flatpak…) → se permite sin más;
* viene de fuera — tiene la marca de descarga del navegador o de KDE (atributo user.xdg.origin.url), o está en
  Descargas, /tmp o un USB — y su SHA-256:
    - ya analizado y limpio (o permitido por el usuario) → se permite: desde aquí se ejecuta normalmente;
    - analizado y malicioso → se deniega siempre;
    - desconocido → se deniega ESTA vez y se analiza (detonar.py: máquina virtual si hay KVM, si no sandbox),
      con la identidad del usuario que lo intentó. El escritorio avisa del resultado.
Seguridad ante fallos: si Escudo se cae, el kernel deja de consultar y todo se ejecuta (nunca bloquea el
sistema); cualquier error interno al decidir también permite.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import logging
import os
import queue
import re
import struct
import threading
import time
from pathlib import Path

from . import detonar, fan, informe
from .behavior import Alert, emit

log = logging.getLogger("escudo.primera_ejecucion")

FAN_CLOEXEC, FAN_CLASS_CONTENT = 0x1, 0x4
FAN_MARK_ADD, FAN_MARK_MOUNT = 0x1, 0x10
FAN_OPEN_EXEC_PERM = 0x00040000
FAN_ALLOW, FAN_DENY = 0x01, 0x02
O_RDONLY, O_LARGEFILE = 0, 0o100000
AT_FDCWD = -100
META = struct.Struct("<IBBHQii")          # fanotify_event_metadata
RESP = struct.Struct("<iI")               # fanotify_response
DET_DIR = Path(os.environ.get("ESCUDO_DETONAR_DIR", "/var/lib/escudo-detonar"))
EXTERNAL_DIRS = re.compile(r"^(/home/[^/]+/(Descargas|Downloads)/|/tmp/|/var/tmp/|/media/|/run/media/)")
ORIGIN_XATTRS = ("user.xdg.origin.url", "user.xdg.referrer.url")


def is_external(path: str, fd: int | None = None, st: os.stat_result | None = None) -> bool:
    """¿Viene de fuera? Marca de descarga (navegador/KDE) o carpeta de descargas, temporal o USB.

    Excepción: un programa de root que solo root puede modificar no es una descarga de un usuario (ningún usuario
    puede crear archivos de root). Sin ella, la puerta bloqueaba al instalador, que ejecuta el sistema nuevo desde
    /tmp/calamares-root-… («chroot: failed to run command 'ln': Operation not permitted»). Con marca de descarga se
    analiza igual."""
    for name in ORIGIN_XATTRS:
        try:
            os.getxattr(fd if fd is not None else path, name)
            return True
        except OSError:
            continue
    if EXTERNAL_DIRS.match(path):
        if st is None:
            try:
                st = os.fstat(fd) if fd is not None else os.stat(path, follow_symlinks=False)
            except OSError:
                return True
        return not (st.st_uid == 0 and not st.st_mode & 0o022)
    return False


def decide(path: str, external: bool, sha: str | None, allowlist: dict) -> str:
    """'permitir' | 'denegar' | 'analizar'"""
    if not external or path.startswith(str(DET_DIR) + "/"):
        return "permitir"
    entry = allowlist.get(sha or "")
    if not entry:
        return "analizar"
    if entry.get("veredicto") == "limpio" or entry.get("permitido_por_usuario"):
        return "permitir"
    return "denegar"


class Gate:
    def __init__(self, quarantine=None, alert=emit, segundos: int = 45, mounts: list[str] | None = None,
                 scanner=None):
        self.fixed_mounts = mounts             # pruebas: vigilar solo estos montajes
        self.quarantine, self.alert, self.segundos = quarantine, alert, segundos
        self.scanner = scanner                 # firmas, YARA y ML: antes de ejecutar nada en el aislamiento
        self.allow = detonar.cargar_permitidos()
        self._hash_cache: dict[tuple, str] = {}
        self._queued: set[str] = set()
        self._jobs: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self.fd = -1
        self._marked: set[str] = set()
        self._devs: set[int] = set()          # dispositivos de las carpetas vigiladas (filtro rápido)

    # ------------------------------------------------------------ fanotify
    def start(self) -> list[threading.Thread]:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self._libc = libc
        self.fd = libc.fanotify_init(FAN_CLOEXEC | FAN_CLASS_CONTENT, O_RDONLY | O_LARGEFILE | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "fanotify_init (¿falta CAP_SYS_ADMIN en el servicio o el kernel no "
                                              "tiene CONFIG_FANOTIFY_ACCESS_PERMISSIONS?)")
        DET_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(DET_DIR, 0o711)
        self.mark_mounts()
        self._desp = fan.Despertador()
        threads = [threading.Thread(target=t, daemon=True, name=n) for t, n in
                   ((self._events, "fanotify"), (self._worker, "detonar"), (self._watch_mounts, "montajes"))]
        self._lectores = [threads[0], threads[2]]       # los que usan self.fd (el de análisis no)
        for t in threads:
            t.start()
        log.info("Puerta de primera ejecución activa en: %s", ", ".join(sorted(self._marked)))
        return threads

    def mark_mounts(self) -> None:
        """Marca el SISTEMA DE ARCHIVOS de /home, /tmp, /var/tmp y las memorias USB (ver fan.py: una marca por
        montaje no ve nada desde el espacio de montajes propio del servicio). Si /home comparte disco con /
        (subvolúmenes de btrfs), la marca ve todas las ejecuciones: las de otros subvolúmenes se permiten al
        instante con un fstat, sin leer su ruta (on_exec)."""
        if self.fixed_mounts is not None:
            wanted = list(self.fixed_mounts)
        else:
            wanted = [p for p in ("/home", "/tmp", "/var/tmp") if os.path.isdir(p)]
        try:
            for line in open("/proc/self/mountinfo"):
                mp = line.split()[4].replace("\\040", " ")
                if self.fixed_mounts is None and mp.startswith(("/media/", "/run/media/")):
                    wanted.append(mp)
        except OSError:
            pass
        wanted += fan.submounts(wanted)
        for mp in wanted:
            if mp in self._marked:
                continue
            if fan.mark(self._libc, self.fd, mp, FAN_OPEN_EXEC_PERM):
                self._marked.add(mp)
        self._devs = fan.devices(sorted(self._marked))

    def _watch_mounts(self) -> None:
        """Memorias USB que se conectan después. El kernel avisa (POLLPRI en /proc/self/mountinfo) cuando cambia
        la tabla de montajes: antes se releía cada 5 s aunque no cambiara nada."""
        import select
        try:
            fh = open("/proc/self/mountinfo", "rb")
            poller = select.poll()
            poller.register(fh, select.POLLPRI | select.POLLERR)
        except (OSError, ValueError):
            fh = poller = None
        if poller is not None:
            poller.register(self._desp.r, select.POLLIN)
        while not self._stop.is_set():
            if poller is None:
                if self._stop.wait(5):
                    return
            else:
                if any(f == self._desp.r for f, _ in poller.poll(600_000)):
                    return
                fh.seek(0)
                fh.read()                              # leer «confirma» el cambio para el próximo aviso
            self.mark_mounts()

    def _events(self) -> None:
        while not self._stop.is_set():
            if not fan.esperar(self.fd, self._desp):
                return
            try:
                buf = os.read(self.fd, 4096)
            except InterruptedError:
                continue
            except OSError as e:
                if e.errno == 9 or self._stop.is_set():          # EBADF: cerrado al parar
                    return
                log.warning("Error leyendo eventos de fanotify (%s): se sigue", e)
                time.sleep(0.2)
                continue
            off = 0
            while off + META.size <= len(buf):
                ev_len, _vers, _res, _mlen, mask, efd, pid = META.unpack_from(buf, off)
                off += ev_len or META.size
                if efd < 0:
                    continue
                verdict = FAN_ALLOW
                try:
                    if mask & FAN_OPEN_EXEC_PERM:
                        verdict = self.on_exec(efd, pid)
                except Exception:  # noqa: BLE001 — ante cualquier error, permitir (nunca bloquear el sistema)
                    log.exception("Error decidiendo una ejecución: se permite")
                    verdict = FAN_ALLOW
                finally:
                    try:
                        os.write(self.fd, RESP.pack(efd, verdict))
                    finally:
                        os.close(efd)

    def _sha(self, efd: int) -> str:
        st = os.fstat(efd)
        key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        if key not in self._hash_cache:
            h, off = hashlib.sha256(), 0
            while chunk := os.pread(efd, 1 << 20, off):
                h.update(chunk)
                off += len(chunk)
            self._hash_cache[key] = h.hexdigest()
        return self._hash_cache[key]

    def on_exec(self, efd: int, pid: int) -> int:
        st = os.fstat(efd)
        if st.st_dev not in self._devs:
            return FAN_ALLOW                     # /usr y demás subvolúmenes: microsegundos, sin leer la ruta
        path = fan.fd_path(efd, st)
        if not fan.inside(path, sorted(self._marked)):
            return FAN_ALLOW
        external = is_external(path, efd, st)
        if not external:
            return FAN_ALLOW
        sha = self._sha(efd)
        d = decide(path, external, sha, self.allow)
        if d == "permitir":
            return FAN_ALLOW
        if d == "analizar" and sha not in self._queued:
            uid, gid = self._owner(pid)
            self._queued.add(sha)
            self._jobs.put((path, sha, uid, gid))
            self.alert(Alert("primera_ejecucion", "low", f"Primera vez que se ejecuta {os.path.basename(path)}: "
                             "se está analizando en un entorno aislado (≈1 min).",
                             pid, path, "Vuelve a abrirlo cuando Escudo avise del resultado."))
        return FAN_DENY

    @staticmethod
    def _owner(pid: int) -> tuple[int, int]:
        uid = gid = 65534
        try:
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("Uid:"):
                    uid = int(line.split()[1])
                elif line.startswith("Gid:"):
                    gid = int(line.split()[1])
        except OSError:
            pass
        return (65534, 65534) if uid == 0 else (uid, gid)   # nunca se analiza como root

    # ------------------------------------------------------------ análisis
    def _worker(self) -> None:
        while not self._stop.is_set():
            path, sha, uid, gid = self._jobs.get()
            try:
                self.analyze(path, sha, uid, gid)
            except Exception:  # noqa: BLE001
                log.exception("Fallo analizando %s", path)
            finally:
                self._queued.discard(sha)

    def analyze(self, path: str, sha: str, uid: int, gid: int) -> detonar.Informe | None:
        try:
            if detonar.sha256_file(path) != sha:
                return None                            # el archivo cambió mientras esperaba: se analizará al ejecutarlo
        except OSError:
            return None
        # Primero los motores estáticos (instantáneos): antes la puerta nunca los consultaba y todo esperaba los
        # 45 s del aislamiento, incluso un malware con firma conocida
        estaticas = []
        if self.scanner is not None:
            try:
                estaticas = [d for d in self.scanner.scan_file(path) if d.severity in ("high", "medium")]
            except OSError:
                estaticas = []
        if any(d.severity == "high" for d in estaticas):
            inf = detonar.Informe(veredicto="malicioso", motivos=[f"detectado: {d.name}" for d in estaticas][:3],
                                  entorno="firmas", sha256=sha)
        else:
            inf = detonar.detonar(path, segundos=self.segundos, uid=uid, gid=gid, base_dir=DET_DIR)
            if estaticas and inf.veredicto == "limpio":          # medio en firmas: nunca sale «limpio»
                inf.veredicto = "sospechoso"
                inf.motivos = [f"detectado: {d.name}" for d in estaticas][:3] + inf.motivos
        detonar.guardar_veredicto(inf, path)
        try:
            informe.guardar(inf.to_dict(), path, uid)        # para «Programas revisados» en TISMA Seguridad
        except OSError as e:
            log.warning("no se pudo guardar el informe: %s", e)
        self.allow = detonar.cargar_permitidos()
        name = os.path.basename(path)
        # En el aviso, frases simples (el detalle técnico queda en el informe de TISMA Seguridad)
        simples = [p for g in informe.explicar(inf.to_dict())["grupos"] for p in g["puntos"]]
        if inf.veredicto == "limpio":
            self.alert(Alert("primera_ejecucion", "low", f"{name}: sin comportamiento malicioso ({inf.entorno}, "
                             f"{inf.segundos:.0f} s).", None, path, "Ya puedes abrirlo con normalidad."))
        elif inf.veredicto == "sospechoso":
            self.alert(Alert("primera_ejecucion", "medium", f"{name}: hizo cosas raras. " +
                             " ".join(simples[:2]), None, path,
                             "No lo abras si no confías en quién te lo dio. Si confías, pulsa «Permitir».",
                             boton="permitir"))
        else:
            action = "bloqueado"
            if self.quarantine is not None:
                try:
                    self.quarantine.add(path, "Escudo.Detonacion." + inf.motivos[0][:40] if inf.motivos else "x")
                    action = "movido a cuarentena"
                except OSError:
                    pass
            self.alert(Alert("primera_ejecucion", "critical", f"{name} es peligroso. " + " ".join(simples[:2]),
                             None, path, action, boton="ver_informe"))
        return inf

    def stop(self) -> None:
        self._stop.set()
        self.fd = fan.parar_hilos(getattr(self, "_lectores", []), self.fd, getattr(self, "_desp", None), log)


def permitir(path: str) -> str:
    """El usuario (como administrador) acepta un programa marcado como sospechoso."""
    sha = detonar.sha256_file(path)
    data = detonar.cargar_permitidos()
    entry = data.get(sha, {"veredicto": "sin analizar", "ruta": path})
    if entry.get("veredicto") == "malicioso":
        raise ValueError("está marcado como MALICIOSO: no se permite desde aquí")
    entry["permitido_por_usuario"] = time.time()
    data[sha] = entry
    p = detonar.ALLOWLIST
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        import json
        json.dump(data, f)
    os.replace(tmp, p)
    return sha
