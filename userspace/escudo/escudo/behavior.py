"""Vigilante de comportamiento: detecta programas que ACTÚAN de forma sospechosa.

Fuentes:
* auditd (el kernel registra el evento con el proceso que lo hizo): ejecución desde /tmp o
  Descargas, lectura de secretos (~/.ssh, llaves, cookies/contraseñas del navegador), escritura
  en lugares de persistencia (cron, systemd, autostart, ld.so.preload, sudoers), ptrace sobre
  otros procesos y carga de módulos del kernel.
* Detector de ransomware: ráfaga de archivos de usuario cuyo contenido deja de corresponder a
  su tipo (un .pdf que ya no empieza por %PDF…) con entropía de datos cifrados. El proceso que
  más escribe en ese momento se CONGELA (SIGSTOP): queda en pausa, no se destruye nada, y el
  usuario decide.

Las alertas se escriben en /run/escudo/alertas.jsonl; el notificador de la sesión las muestra.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import stat
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .scanner import shannon_entropy

log = logging.getLogger("escudo.behavior")
ALERTS = Path(os.environ.get("ESCUDO_ALERTS", "/run/escudo/alertas.jsonl"))
AUDIT_LOG = "/var/log/audit/audit.log"

# Programas legítimos para cada tipo de acceso (rutas reales de ejecutable)
SECRET_READERS = {"/usr/bin/ssh", "/usr/bin/ssh-add", "/usr/bin/ssh-agent", "/usr/bin/scp", "/usr/bin/sftp",
                  "/usr/sbin/sshd", "/usr/bin/git", "/usr/lib/git-core/git-remote-http", "/usr/bin/gpg",
                  "/usr/bin/gpg-agent", "/usr/bin/gpg2", "/usr/bin/ssh-keygen", "/usr/bin/rsync",
                  "/usr/lib/openssh/ssh-sk-helper"}
BROWSER_FILES = re.compile(r"/(Cookies|Login Data|Web Data|key4\.db|logins\.json|cookies\.sqlite)$")
BROWSERS = {"/usr/lib/chromium/chromium", "/usr/lib/firefox-esr/firefox-esr", "/usr/bin/chromium",
            "/usr/lib/firefox-esr/firefox-esr-bin"}
PERSISTENCE_WRITERS = {"/usr/bin/crontab", "/usr/bin/systemctl", "/usr/bin/dpkg", "/usr/bin/apt",
                       "/usr/bin/apt-get", "/usr/lib/apt/methods/http", "/usr/bin/plasmashell",
                       "/usr/bin/kwriteconfig6", "/usr/bin/systemsettings", "/usr/bin/visudo"}

SEVERITY = {"exec_tmp": "medium", "secretos": "high", "persistencia": "medium", "ldpreload": "high",
            "ptrace": "medium", "modulos": "high", "navegador_secretos": "high", "ransomware": "critical"}


@dataclass
class Alert:
    kind: str
    severity: str
    detail: str
    pid: int | None = None
    exe: str | None = None
    action: str = "alerta"
    time: float = field(default_factory=time.time)


def emit(alert: Alert, path: Path = ALERTS) -> None:
    log.warning("[%s] %s: %s (pid=%s exe=%s) → %s", alert.severity.upper(), alert.kind, alert.detail,
                alert.pid, alert.exe, alert.action)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(alert), ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- auditd
_FIELD = re.compile(r'(\w+)=("([^"]*)"|\S+)')
_SERIAL = re.compile(r"msg=audit\([\d.]+:(\d+)\)")


def parse_fields(line: str) -> dict[str, str]:
    out = {}
    for m in _FIELD.finditer(line):
        k, raw, quoted = m.group(1), m.group(2), m.group(3)
        v = quoted if quoted is not None else raw
        if quoted is None and re.fullmatch(r"[0-9A-F]+", v) and len(v) % 2 == 0 and len(v) >= 4 and k in ("name", "exe", "comm", "proctitle"):
            try:
                v = bytes.fromhex(v).decode("utf-8", "replace")   # auditd codifica en hex lo «raro»
            except ValueError:
                pass
        out.setdefault(k, v)
    return out


def group_events(lines) -> dict[str, list[dict]]:
    events: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        m = _SERIAL.search(line)
        if m:
            f = parse_fields(line)
            f["_type"] = line.split(" ", 1)[0].removeprefix("type=")
            events[m.group(1)].append(f)
    return events


def evaluate(records: list[dict]) -> Alert | None:
    """Convierte un evento de auditd (varios registros con el mismo número) en una alerta, o None."""
    sc = next((r for r in records if r["_type"] == "SYSCALL"), None)
    if not sc:
        return None
    key = sc.get("key", "").strip('"')
    exe, pid = sc.get("exe"), int(sc.get("pid", 0) or 0)
    names = [r.get("name", "") for r in records if r["_type"] == "PATH" and r.get("name")]
    target = next((n for n in reversed(names) if n.startswith("/")), names[-1] if names else "")
    if key == "secretos":
        if exe in SECRET_READERS:
            return None
        return Alert("secretos", SEVERITY[key], f"{exe} leyó {target}", pid, exe)
    if key == "navegador":
        if exe in BROWSERS or not BROWSER_FILES.search(target):
            return None
        return Alert("navegador_secretos", SEVERITY["navegador_secretos"],
                     f"{exe} accedió a datos del navegador ({target})", pid, exe)
    if key == "persistencia":
        if exe in PERSISTENCE_WRITERS:
            return None
        return Alert("persistencia", SEVERITY[key], f"{exe} modificó {target}", pid, exe)
    if key in ("exec_tmp", "ldpreload", "ptrace", "modulos"):
        detail = {"exec_tmp": f"se ejecutó {exe} desde una carpeta temporal o de descargas",
                  "ldpreload": f"{exe} modificó /etc/ld.so.preload",
                  "ptrace": f"{exe} intentó depurar/inyectarse en otro proceso",
                  "modulos": f"{exe} cargó un módulo del kernel"}[key]
        if key == "ptrace" and sc.get("success") == "no":
            detail += " (bloqueado por Yama)"
        return Alert(key, SEVERITY[key], detail, pid, exe)
    return None


def follow(path: str, stop: threading.Event):
    with open(path, encoding="utf-8", errors="replace") as fh:
        fh.seek(0, os.SEEK_END)
        while not stop.is_set():
            line = fh.readline()
            if line:
                yield line
            else:
                time.sleep(0.3)


def watch_audit(stop: threading.Event, path: str = AUDIT_LOG, on_alert=emit) -> None:
    pending: dict[str, list[dict]] = defaultdict(list)
    order: deque = deque()
    for line in follow(path, stop):
        m = _SERIAL.search(line)
        if not m:
            continue
        serial = m.group(1)
        if serial not in pending:
            order.append(serial)
        pending[serial].extend(group_events([line])[serial])
        if line.startswith("type=EOE") or len(order) > 64:     # fin de evento
            done = serial if line.startswith("type=EOE") else order[0]
            recs = pending.pop(done, [])
            try:
                order.remove(done)
            except ValueError:
                pass
            alert = evaluate(recs)
            if alert:
                on_alert(alert)


# ---------------------------------------------------------------- ransomware
MAGIC = {".pdf": [b"%PDF"], ".png": [b"\x89PNG"], ".jpg": [b"\xff\xd8\xff"], ".jpeg": [b"\xff\xd8\xff"],
         ".gif": [b"GIF8"], ".zip": [b"PK"], ".docx": [b"PK"], ".xlsx": [b"PK"], ".pptx": [b"PK"],
         ".odt": [b"PK"], ".ods": [b"PK"], ".odp": [b"PK"], ".mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3"],
         ".mp4": [b"ftyp"], ".stl": [b"solid", b""], ".7z": [b"7z\xbc\xaf"], ".gz": [b"\x1f\x8b"]}
TEXT_EXT = {".txt", ".md", ".csv", ".py", ".c", ".h", ".cpp", ".js", ".ts", ".html", ".json", ".yaml",
            ".yml", ".tex", ".gcode", ".sh", ".xml", ".ini", ".toml", ".rs", ".go", ".java"}


def looks_encrypted(path: str, head: bytes) -> bool:
    """True si el contenido ya no corresponde a su tipo y parece cifrado."""
    if len(head) < 1024:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in head[:4096]) / min(len(head), 4096)
        if printable > 0.85:
            return False
        try:
            head[:4096].decode("utf-8")
            return False
        except UnicodeDecodeError:
            pass
    elif ext in MAGIC:
        sig = MAGIC[ext]
        if ext == ".mp4":
            if b"ftyp" in head[:16]:
                return False
        elif any(s and head.startswith(s) for s in sig) or (ext == ".stl" and head[80:84]):
            return False
    else:
        return False                      # tipo desconocido: no opinamos
    return shannon_entropy(head[:65536]) > 7.2


def top_writers(sample_s: float = 1.0, uid: int | None = None) -> list[tuple[int, int, str]]:
    """Procesos que más bytes escriben ahora (pid, bytes, exe), del usuario indicado."""
    def snap():
        out = {}
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                if uid is not None and d.stat().st_uid != uid:
                    continue
                wb = next(int(line.split()[1]) for line in (d / "io").read_text().splitlines()
                          if line.startswith("write_bytes"))
                out[int(d.name)] = wb
            except (OSError, StopIteration, ValueError):
                continue
        return out
    a = snap()
    time.sleep(sample_s)
    b = snap()
    res = []
    for pid, wb in b.items():
        delta = wb - a.get(pid, wb)
        if delta > 0:
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                exe = "?"
            res.append((pid, delta, exe))
    return sorted(res, key=lambda t: -t[1])


PROTECTED_EXES = {"/usr/lib/systemd/systemd", "/usr/bin/plasmashell", "/usr/bin/kwin_wayland",
                  "/usr/bin/Xwayland", "/usr/lib/xorg/Xorg"}


class RansomwareGuard:
    """Muchos archivos de un mismo usuario que pasan a parecer cifrados en pocos segundos → se congela (SIGSTOP)
    el proceso de ESE usuario que más escribe. Nunca procesos de root ni del sistema (solo alerta)."""

    def __init__(self, threshold: int = 15, window_s: float = 10.0, freeze: bool = True, on_alert=emit,
                 writers=top_writers):
        self.threshold, self.window, self.freeze = threshold, window_s, freeze
        self.hits: dict[int, deque] = {}          # por usuario: la actividad de otro no suma
        self.on_alert, self.writers = on_alert, writers
        self._cooldown: dict[int, float] = {}
        self.protect_root = True                  # procesos de root: solo alerta (las pruebas corren como root)

    @staticmethod
    def _read_head(path: str) -> tuple[bytes, int] | None:
        """Lee el principio del archivo SIN seguir enlaces ni bloquearse en FIFO/dispositivos (corre como root)."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_NOATIME", 0))
        except OSError:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None
            return os.read(fd, 65536), st.st_uid
        except OSError:
            return None
        finally:
            os.close(fd)

    def _freeze(self, pid: int, exe: str, uid: int) -> str:
        """Congela solo si el proceso sigue siendo el mismo (pidfd: inmune a la reutilización del PID), su
        ejecutable coincide y pertenece al usuario de los archivos."""
        if uid == 0 and self.protect_root:
            return "alerta (proceso del sistema: no se congela)"
        try:
            pidfd = os.pidfd_open(pid)
        except OSError as e:
            return f"no se pudo congelar: {e}"
        try:
            real_exe = os.readlink(f"/proc/{pid}/exe")
            puid = int(next(line.split()[1] for line in open(f"/proc/{pid}/status") if line.startswith("Uid:")))
            if real_exe != exe or puid != uid:
                return "alerta (el proceso cambió antes de poder congelarlo)"
            signal.pidfd_send_signal(pidfd, signal.SIGSTOP)
            return f"proceso {pid} CONGELADO (reanudar: kill -CONT {pid})"
        except (OSError, StopIteration, ValueError) as e:
            return f"no se pudo congelar: {e}"
        finally:
            os.close(pidfd)

    def file_changed(self, path: str, now: float | None = None) -> Alert | None:
        now = time.time() if now is None else now
        got = self._read_head(path)
        if not got:
            return None
        head, uid = got
        if not looks_encrypted(path, head):
            return None
        hits = self.hits.setdefault(uid, deque())
        hits.append(now)
        while hits and hits[0] < now - self.window:
            hits.popleft()
        if len(hits) < self.threshold or now < self._cooldown.get(uid, 0):
            return None
        self._cooldown[uid] = now + 30
        culprit = next(((pid, exe) for pid, _, exe in self.writers(uid=uid) if exe not in PROTECTED_EXES), None)
        action = "alerta"
        if culprit and self.freeze:
            action = self._freeze(culprit[0], culprit[1], uid)
        alert = Alert("ransomware", "critical",
                      f"{len(hits)} archivos parecen cifrados en {self.window:.0f} s (último: {path})",
                      culprit[0] if culprit else None, culprit[1] if culprit else None, action)
        self.on_alert(alert)
        return alert
