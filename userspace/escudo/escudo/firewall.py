"""Integración con el firewall del sistema para bloquear IPs atacantes.

- Linux: nftables, conjuntos ``bloqueadas4``/``bloqueadas6`` de la tabla
  ``inet centinela`` (con expiración gestionada por el propio kernel).
- Windows: reglas de Windows Defender Firewall vía ``netsh advfirewall``.

Todas las IPs se validan con ``ipaddress`` y los comandos se ejecutan sin shell,
por lo que no es posible inyectar comandos a través de datos de logs.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import subprocess
import threading
import time
from pathlib import Path

from .config import IS_WINDOWS, Config

log = logging.getLogger("escudo.firewall")
TABLE = ["inet", "centinela"]
# Tabla mínima para sistemas que no traen /etc/nftables.conf de Centinela.
# Solo se añade lo que falte («add» no falla si ya existe): no se redeclara la cadena input de nftables.conf.
NFT_SETS = """
add table inet centinela
add set inet centinela bloqueadas4 { type ipv4_addr; flags timeout; }
add set inet centinela bloqueadas6 { type ipv6_addr; flags timeout; }
"""
NFT_CHAIN = """
add chain inet centinela input { type filter hook input priority filter; policy accept; }
add rule inet centinela input ip saddr @bloqueadas4 drop
add rule inet centinela input ip6 saddr @bloqueadas6 drop
"""
RULE_PREFIX = "Escudo-Block-"


def essential_ips() -> set[str]:
    out = set()
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            f = line.split()
            if len(f) > 2 and f[1] == "00000000" and f[2] != "00000000":
                out.add(str(ipaddress.IPv4Address(bytes.fromhex(f[2])[::-1])))
    except (OSError, ValueError):
        pass
    # Puerta de enlace IPv6 (siguiente salto de la ruta por defecto ::/0)
    try:
        for line in Path("/proc/net/ipv6_route").read_text().splitlines():
            f = line.split()
            if len(f) >= 5 and f[0] == "0" * 32 and f[1] == "00" and f[4] != "0" * 32:
                out.add(str(ipaddress.IPv6Address(bytes.fromhex(f[4]))))
    except (OSError, ValueError):
        pass
    # Servidores de las VPN de tisma-vpn: bloquearlos cortaría el túnel
    for ep in Path("/etc/tisma-vpn").glob("*.endpoint"):
        try:
            host = ep.read_text().strip().rsplit(":", 1)[0].strip("[]")
            out.add(str(ipaddress.ip_address(host)))
        except (OSError, ValueError):
            pass
    for conf in ("/etc/resolv.conf", "/run/systemd/resolve/resolv.conf"):
        try:
            for line in Path(conf).read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    out.add(str(ipaddress.ip_address(parts[1].split("%")[0])))
        except (OSError, ValueError):
            pass
    return out


def normalize_ip(ip: str) -> str:
    return str(ipaddress.ip_address(ip.strip().strip("[]")))


class Firewall:
    def __init__(self, config: Config):
        self.config = config
        self.dry_run = config.dry_run_firewall
        self.state_file: Path = config.blocklist_file
        self._lock = threading.Lock()
        self.blocked: dict[str, float] = {}  # ip -> expiración (0 = permanente)
        if self.state_file.is_file():
            self.blocked = json.loads(self.state_file.read_text(encoding="utf-8"))
        self._whitelist = [ipaddress.ip_network(w, strict=False) for w in config.whitelist_ips]
        # Nunca bloquear la puerta de enlace ni los DNS: un atacante podría dejarnos sin red
        self._whitelist += [ipaddress.ip_network(ip) for ip in essential_ips()]
        self._chain_ready = False

    # ------------------------------------------------------------------ util
    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        if self.dry_run:
            log.info("[dry-run] %s", " ".join(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, capture_output=True, text=True, check=check, timeout=30)

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.blocked, indent=1), encoding="utf-8")

    def is_whitelisted(self, ip: str) -> bool:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in self._whitelist)

    @staticmethod
    def _set(ip: str) -> str:
        return "bloqueadas6" if ipaddress.ip_address(ip).version == 6 else "bloqueadas4"

    def _ensure_table(self) -> None:
        if self._chain_ready or IS_WINDOWS:
            return
        if not self.dry_run:
            chain = subprocess.run(["nft", "list", "chain", *TABLE, "input"], capture_output=True, timeout=30)
            script = NFT_SETS + ("" if chain.returncode == 0 else NFT_CHAIN)
            r = subprocess.run(["nft", "-f", "-"], input=script, text=True, capture_output=True, timeout=30)
            if r.returncode != 0:
                log.error("No se pudieron preparar los conjuntos de bloqueo de nftables: %s", r.stderr.strip())
                return
        self._chain_ready = True

    # ------------------------------------------------------------------ API
    def block(self, ip: str, duration_s: int | None = None, reason: str = "") -> bool:
        ip = normalize_ip(ip)
        if self.is_whitelisted(ip):
            log.warning("No se bloquea %s: está en la lista blanca", ip)
            return False
        duration = self.config.block_duration_s if duration_s is None else duration_s
        with self._lock:
            exp = self.blocked.get(ip)
            if exp is not None and (exp == 0 or exp > time.time()):
                return False                     # ya bloqueada y vigente (una caducada se vuelve a bloquear)
            if IS_WINDOWS:
                for direction in ("in", "out"):
                    self._run(["netsh", "advfirewall", "firewall", "add", "rule",
                               f"name={RULE_PREFIX}{ip}", f"dir={direction}",
                               "action=block", f"remoteip={ip}", "enable=yes"])
            else:
                self._ensure_table()
                elem = f"{ip} timeout {duration}s" if duration else ip
                self._run(["nft", "add", "element", *TABLE, self._set(ip), "{", elem, "}"])
            self.blocked[ip] = time.time() + duration if duration else 0
            self._save()
        log.warning("IP bloqueada: %s (%s)", ip, reason or "manual")
        return True

    def unblock(self, ip: str) -> bool:
        ip = normalize_ip(ip)
        with self._lock:
            if IS_WINDOWS:
                self._run(["netsh", "advfirewall", "firewall", "delete", "rule",
                           f"name={RULE_PREFIX}{ip}"], check=False)
            else:
                self._run(["nft", "delete", "element", *TABLE, self._set(ip), "{", ip, "}"], check=False)
            existed = self.blocked.pop(ip, None) is not None
            self._save()
        if existed:
            log.info("IP desbloqueada: %s", ip)
        return existed

    def expire(self) -> list[str]:
        now = time.time()
        expired = [ip for ip, exp in list(self.blocked.items()) if exp and exp <= now]
        for ip in expired:
            self.unblock(ip)
        return expired

    def restore(self) -> int:
        """Los conjuntos de nftables se pierden al reiniciar: reinstala los bloqueos vigentes del estado guardado
        con el tiempo que les queda (sin esto, tras un reinicio las IPs seguían «bloqueadas» solo en el JSON)."""
        if IS_WINDOWS:
            return 0
        self.expire()
        now, n = time.time(), 0
        with self._lock:
            for ip, exp in list(self.blocked.items()):
                try:
                    ip = normalize_ip(ip)
                except ValueError:
                    self.blocked.pop(ip, None)
                    continue
                if self.is_whitelisted(ip):
                    self.blocked.pop(ip, None)   # p. ej. ahora es la puerta de enlace
                    continue
                self._ensure_table()
                elem = f"{ip} timeout {max(1, int(exp - now))}s" if exp else ip
                self._run(["nft", "add", "element", *TABLE, self._set(ip), "{", elem, "}"], check=False)
                n += 1
            self._save()
        return n

    def block_inbound_port(self, port: int) -> None:
        """Cierra un puerto entrante (p. ej. 23/telnet, 445/SMB expuesto)."""
        if not 0 < port < 65536:
            raise ValueError("Puerto inválido")
        if IS_WINDOWS:
            for proto in ("TCP", "UDP"):
                self._run(["netsh", "advfirewall", "firewall", "add", "rule",
                           f"name=Escudo-Port-{port}-{proto}", "dir=in", "action=block",
                           f"protocol={proto}", f"localport={port}"])
        else:
            self._ensure_table()
            for proto in ("tcp", "udp"):
                self._run(["nft", "insert", "rule", *TABLE, "input", proto, "dport", str(port), "drop"])
