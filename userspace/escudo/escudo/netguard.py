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
AUTH_PATTERNS = [re.compile(p) for p in (
    rf"Failed password for (invalid user )?\S+ from {_IP}",
    rf"Invalid user \S* from {_IP}",
    rf"authentication failure;.*rhost={_IP}",
    rf"Connection closed by (invalid|authenticating) user \S+ {_IP} port \d+ \[preauth\]",
    rf"Did not receive identification string from {_IP}",
    rf"maximum authentication attempts exceeded for .* from {_IP}",
    rf"Unable to negotiate with {_IP}",
    rf"Disconnected from invalid user \S+ {_IP}",
)]
LINUX_AUTH_LOGS = ["/var/log/auth.log", "/var/log/secure"]


def extract_attacker_ip(line: str) -> str | None:
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
    a = ipaddress.ip_address(ip)
    return not (a.is_loopback or a.is_unspecified or a.is_multicast)


def _proc_net_connections() -> list[tuple[str, int, str]]:
    """Fallback sin psutil en Linux: lee /proc/net/tcp{,6}. Devuelve (ip_remota, puerto_local, estado)."""
    states = {"01": "ESTABLISHED", "03": "SYN_RECV"}
    out = []
    for fname, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            lines = Path(fname).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            local, remote, st = parts[1], parts[2], parts[3]
            if st not in states:
                continue
            lport = int(local.split(":")[1], 16)
            rhex = remote.split(":")[0]
            raw = bytes.fromhex(rhex)
            if v6:
                raw = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
                addr = ipaddress.IPv6Address(raw)
                ip = str(addr.ipv4_mapped or addr)
            else:
                ip = str(ipaddress.IPv4Address(raw[::-1]))
            out.append((ip, lport, states[st]))
    return out


def inbound_connections() -> list[tuple[str, int, str]]:
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None
    if psutil:
        listening = {c.laddr.port for c in psutil.net_connections("inet") if c.status == "LISTEN"}
        res = []
        for c in psutil.net_connections("inet"):
            if c.raddr and c.status in ("ESTABLISHED", "SYN_RECV"):
                # Conexión entrante = puerto local en escucha, o SYN_RECV (handshake entrante)
                if c.laddr.port in listening or c.status == "SYN_RECV":
                    res.append((c.raddr.ip, c.laddr.port, c.status))
        return res
    if not IS_WINDOWS:
        return _proc_net_connections()
    return []


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
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            inode = os.fstat(fh.fileno()).st_ino
            while not self._stop.is_set():
                line = fh.readline()
                if line:
                    self.process_log_line(line)
                    continue
                time.sleep(0.5)
                try:  # rotación de logs
                    if os.stat(path).st_ino != inode:
                        return self._follow_file(path)
                except FileNotFoundError:
                    pass

    def _follow_journald(self) -> None:
        proc = subprocess.Popen(
            ["journalctl", "-f", "-n", "0", "-o", "cat", "_COMM=sshd", "+", "_COMM=sshd-session"],
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
            try:
                for ip, port, _ in inbound_connections():
                    self.handle_connection(ip, port)
            except Exception as e:  # noqa: BLE001
                log.debug("Error leyendo conexiones: %s", e)
            self.fw.expire()
            self._stop.wait(2)

    # --------------------------------------------------------- ciclo
    def start(self) -> list[threading.Thread]:
        for port in self.config.blocked_ports_inbound:
            self.fw.block_inbound_port(int(port))
        targets = [self._watch_connections]
        if IS_WINDOWS:
            targets.append(self._follow_windows_events)
        else:
            logs = [p for p in LINUX_AUTH_LOGS if os.access(p, os.R_OK)]
            if logs:
                targets.extend(lambda p=p: self._follow_file(p) for p in logs)
            elif shutil.which("journalctl"):
                targets.append(self._follow_journald)
            else:
                log.warning("No se encontró log de autenticación legible")
        threads = [threading.Thread(target=t, daemon=True) for t in targets]
        for t in threads:
            t.start()
        log.info("Protección de red activa (%d fuentes)", len(threads))
        return threads

    def stop(self) -> None:
        self._stop.set()
