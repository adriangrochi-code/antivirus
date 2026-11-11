"""Prevención de intrusiones (IPS): detecta ataques externos y bloquea al atacante.

Detecta:
- Fuerza bruta contra SSH / login (Linux: auth.log, secure o journald).
- Fuerza bruta contra RDP/SMB/inicios de sesión de Windows (evento 4625).
- Escaneo de puertos (una IP que toca muchos puertos locales en poco tiempo).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from .config import IS_WINDOWS, Config
from .firewall import Firewall

log = logging.getLogger("escudo.netguard")

_IP = r"(?P<ip>[0-9a-fA-F:.]{3,45})"
# Anclados al formato exacto de sshd y con la IP real AL FINAL: la parte que controla el atacante
# (el nombre de usuario) es «.*» voraz, así que una IP escrita dentro del usuario nunca se toma.
AUTH_PATTERNS = [re.compile(p) for p in (
    rf"^Failed (?:password|publickey|keyboard-interactive/pam|none) for (?:invalid user )?.* from {_IP} port \d+(?: ssh2)?$",
    rf"^Invalid user .* from {_IP} port \d+$",
    rf"^pam_unix\(sshd:auth\): authentication failure; logname=\S* uid=\d+ euid=\d+ tty=\S* ruser=\S* rhost={_IP}(?:\s+user=.*)?$",
    rf"^Connection closed by (?:invalid|authenticating) user .* {_IP} port \d+ \[preauth\]$",
    rf"^Did not receive identification string from {_IP} port \d+$",
    rf"^maximum authentication attempts exceeded for .* from {_IP} port \d+ ssh2 \[preauth\]$",
    rf"^Unable to negotiate with {_IP} port \d+: ",
    rf"^Disconnected from invalid user .* {_IP} port \d+ \[preauth\]$",
)]
# En los archivos de log (syslog) cualquier usuario puede escribir con «logger -t sshd»:
# solo se aceptan líneas cuyo PID corresponde AHORA a un sshd real.
_SYSLOG = re.compile(r"\b(sshd(?:-session)?)\[(\d+)\]: (.*)$")
SSHD_EXES = {"/usr/sbin/sshd", "/usr/lib/openssh/sshd-session", "/usr/lib/openssh/sshd-auth"}
LINUX_AUTH_LOGS = ["/var/log/auth.log", "/var/log/secure"]


def message_from_syslog(line: str) -> str | None:
    """Mensaje de una línea de auth.log solo si la emitió un proceso sshd real."""
    m = _SYSLOG.search(line.rstrip("\n"))
    if not m:
        return None
    try:
        if os.readlink(f"/proc/{m.group(2)}/exe") not in SSHD_EXES:
            return None
    except OSError:
        return None          # el proceso ya no existe: no se puede verificar el origen
    return m.group(3)


def extract_attacker_ip(line: str) -> str | None:
    line = line.rstrip("\n")
    for pat in AUTH_PATTERNS:
        m = pat.search(line)
        if m:
            try:
                return str(ipaddress.ip_address(m.group("ip")))
            except ValueError:
                return None
    return None


class SlidingCounter:
    """Cuenta eventos por clave dentro de una ventana de tiempo."""

    def __init__(self, threshold: int, window_s: int):
        self.threshold = threshold
        self.window = window_s
        self.events: dict[str, deque] = defaultdict(deque)

    def add(self, key: str, ts: float | None = None) -> bool:
        ts = time.time() if ts is None else ts
        q = self.events[key]
        q.append(ts)
        while q and q[0] < ts - self.window:
            q.popleft()
        if len(q) >= self.threshold:
            q.clear()
            return True
        return False


class PortScanDetector:
    def __init__(self, threshold: int, window_s: int):
        self.threshold = threshold
        self.window = window_s
        self.seen: dict[str, dict[int, float]] = defaultdict(dict)

    def add(self, ip: str, port: int, ts: float | None = None) -> bool:
        ts = time.time() if ts is None else ts
        ports = self.seen[ip]
        ports[port] = ts
        for p, t in list(ports.items()):
            if t < ts - self.window:
                del ports[p]
        if len(ports) >= self.threshold:
            ports.clear()
            return True
        return False


def _is_public(ip: str) -> bool:
    """Solo direcciones globales cuentan para el «escaneo de puertos»: un SYN se puede falsificar, y bloquear
    por eso el router, el NAS o la VPN de la red local sería una denegación de servicio autoinfligida. Los fallos
    de autenticación (handshake completo, no falsificable) sí se bloquean también en la red local."""
    a = ipaddress.ip_address(ip)
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global


LOOPBACK = {"0100007F", "00000000000000000000000001000000"}


def _read_proc_net() -> list[tuple[list[str], bool]]:
    rows = []
    for fname, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            rows += [(line.split(), v6) for line in Path(fname).read_text().splitlines()[1:]]
        except OSError:
            continue
    return rows


def external_listeners(rows: list[tuple[list[str], bool]] | None = None) -> bool:
    """¿Hay algún puerto TCP en escucha fuera de loopback? Si no, nadie puede conectarse desde fuera."""
    for parts, _ in (rows if rows is not None else _read_proc_net()):
        if len(parts) > 3 and parts[3] == "0A" and parts[1].split(":")[0] not in LOOPBACK:
            return True
    return False


def _proc_net_connections(rows: list[tuple[list[str], bool]] | None = None) -> list[tuple[str, int, str]]:
    """Lee /proc/net/tcp{,6} UNA vez. Devuelve (ip_remota, puerto_local, estado) SOLO de conexiones entrantes
    (puerto local en escucha, o handshake entrante SYN_RECV): las salientes (descargas, navegador) nunca
    cuentan como escaneo. Antes se usaba psutil.net_connections dos veces cada 2 s, que como root recorre
    los descriptores de TODOS los procesos para saber de quién es cada conexión (dato que aquí no se usa)."""
    rows = rows if rows is not None else _read_proc_net()
    states = {"01": "ESTABLISHED", "03": "SYN_RECV"}
    listening = {int(p[1].split(":")[1], 16) for p, _ in rows if len(p) > 3 and p[3] == "0A"}
    out = []
    for parts, v6 in rows:
        if len(parts) < 4 or parts[3] not in states:
            continue
        local, remote, st = parts[1], parts[2], parts[3]
        lport = int(local.split(":")[1], 16)
        raw = bytes.fromhex(remote.split(":")[0])
        if v6:
            raw = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
            addr = ipaddress.IPv6Address(raw)
            ip = str(addr.ipv4_mapped or addr)
        else:
            ip = str(ipaddress.IPv4Address(raw[::-1]))
        if states[st] == "SYN_RECV" or lport in listening:
            out.append((ip, lport, states[st]))
    return out


def inbound_connections() -> list[tuple[str, int, str]]:
    if not IS_WINDOWS:
        return _proc_net_connections()
    try:
        import psutil  # type: ignore
    except ImportError:
        return []
    conns = psutil.net_connections("inet")
    listening = {c.laddr.port for c in conns if c.status == "LISTEN"}
    return [(c.raddr.ip, c.laddr.port, c.status) for c in conns
            if c.raddr and c.status in ("ESTABLISHED", "SYN_RECV") and (c.laddr.port in listening or c.status == "SYN_RECV")]


class NetGuard:
    def __init__(self, config: Config, firewall: Firewall | None = None):
        self.config = config
        self.fw = firewall or Firewall(config)
        self.bruteforce = SlidingCounter(config.bruteforce_threshold, config.bruteforce_window_s)
        self.portscan = PortScanDetector(config.portscan_threshold, config.portscan_window_s)
        self._stop = threading.Event()

    # --------------------------------------------------------- reacción
    def handle_auth_failure(self, ip: str, source: str) -> None:
        if self.fw.is_whitelisted(ip):
            return
        if self.bruteforce.add(ip):
            self.fw.block(ip, reason=f"fuerza bruta {source}")

    def handle_connection(self, ip: str, port: int) -> None:
        if not _is_public(ip) or self.fw.is_whitelisted(ip):
            return
        if self.portscan.add(ip, port):
            self.fw.block(ip, reason="escaneo de puertos")

    def process_log_line(self, line: str, source: str = "ssh") -> None:
        ip = extract_attacker_ip(line)
        if ip:
            self.handle_auth_failure(ip, source)

    # --------------------------------------------------------- fuentes
    def _follow_file(self, path: str) -> None:
        from .inotify import Watch
        watch = Watch(os.path.dirname(path) or ".", fallback_s=0.5)   # despierta solo si el registro cambia
        first = True
        while not self._stop.is_set():                # bucle (no recursión) para sobrevivir a las rotaciones
            try:
                fh = open(path, "r", encoding="utf-8", errors="replace")
            except OSError:
                time.sleep(5)
                continue
            with fh:
                if first:
                    fh.seek(0, os.SEEK_END)          # al arrancar, solo lo nuevo; tras rotar, desde el principio
                    first = False
                inode = os.fstat(fh.fileno()).st_ino
                while not self._stop.is_set():
                    line = fh.readline()
                    if line:
                        msg = message_from_syslog(line)
                        if msg:
                            self.process_log_line(msg)
                        continue
                    watch.wait(30)
                    try:
                        if os.stat(path).st_ino != inode:
                            break                    # rotado: reabrir
                    except FileNotFoundError:
                        pass

    def _follow_journald(self) -> None:
        while not self._stop.is_set():                # si journalctl muere, se relanza (antes el IPS callaba)
            try:
                self._follow_journald_once()
            except Exception:  # noqa: BLE001
                log.exception("Fallo leyendo journald; reintento en 10 s")
            self._stop.wait(10)

    def _follow_journald_once(self) -> None:
        proc = subprocess.Popen(
            # _UID=0 lo pone el kernel/journald: un usuario no puede falsificarlo con «logger»
            ["journalctl", "-f", "-n", "0", "-o", "cat", "_UID=0", "_COMM=sshd", "+",
             "_UID=0", "_COMM=sshd-session"],
            stdout=subprocess.PIPE, text=True, errors="replace")
        assert proc.stdout
        for line in proc.stdout:
            if self._stop.is_set():
                break
            self.process_log_line(line)
        proc.terminate()

    def _follow_windows_events(self) -> None:
        """Lee eventos 4625 (inicio de sesión fallido) del registro de Seguridad."""
        last_id = 0
        rx_rec = re.compile(r"<EventRecordID>(\d+)</EventRecordID>")
        rx_ip = re.compile(r"<Data Name='IpAddress'>([^<]+)</Data>")
        while not self._stop.is_set():
            q = f"*[System[(EventID=4625) and (EventRecordID>{last_id})]]"
            try:
                out = subprocess.run(
                    ["wevtutil", "qe", "Security", f"/q:{q}", "/f:xml", "/c:200"],
                    capture_output=True, text=True, timeout=30).stdout
            except (OSError, subprocess.SubprocessError) as e:
                log.error("No se pudo leer el registro de eventos: %s", e)
                return
            for ev in out.split("</Event>"):
                rec = rx_rec.search(ev)
                if not rec:
                    continue
                rid = int(rec.group(1))
                if last_id == 0:
                    # primera pasada: solo fija el punto de partida
                    last_id = max(last_id, rid)
                    continue
                last_id = max(last_id, rid)
                m = rx_ip.search(ev)
                if m:
                    try:
                        ip = str(ipaddress.ip_address(m.group(1)))
                    except ValueError:
                        continue
                    self.handle_auth_failure(ip, "windows-logon")
            self._stop.wait(5)

    def _watch_connections(self) -> None:
        while not self._stop.is_set():
            wait = 2
            try:
                if IS_WINDOWS:
                    conns = inbound_connections()
                else:
                    rows = _read_proc_net()
                    conns = _proc_net_connections(rows)
                    # Sin puertos abiertos al exterior (lo normal en un escritorio) nadie puede entrar:
                    # basta con mirar cada 15 s si alguien abrió uno (antes: cada 2 s siempre)
                    if not conns and not external_listeners(rows):
                        wait = 15
                for ip, port, _ in conns:
                    self.handle_connection(ip, port)
            except Exception as e:  # noqa: BLE001
                log.debug("Error leyendo conexiones: %s", e)
            self.fw.expire()
            self._stop.wait(wait)

    # --------------------------------------------------------- ciclo
    def start(self) -> list[threading.Thread]:
        try:
            n = self.fw.restore()
            if n:
                log.info("Reinstalados %d bloqueos de IP guardados", n)
        except Exception:  # noqa: BLE001
            log.exception("No se pudieron reinstalar los bloqueos guardados")
        for port in self.config.blocked_ports_inbound:
            self.fw.block_inbound_port(int(port))
        targets = [self._watch_connections]
        if IS_WINDOWS:
            targets.append(self._follow_windows_events)
        else:
            logs = [p for p in LINUX_AUTH_LOGS if os.access(p, os.R_OK)]
            if shutil.which("journalctl"):          # preferido: campos de confianza (_UID)
                targets.append(self._follow_journald)
            elif logs:
                targets.extend(lambda p=p: self._follow_file(p) for p in logs)
            else:
                log.warning("No se encontró log de autenticación legible")
        threads = [threading.Thread(target=t, daemon=True) for t in targets]
        for t in threads:
            t.start()
        log.info("Protección de red activa (%d fuentes)", len(threads))
        return threads

    def stop(self) -> None:
        self._stop.set()
