"""Configuración: valores por defecto + archivo JSON opcional."""

from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"


def default_data_dir() -> Path:
    env = os.environ.get("ESCUDO_HOME")
    if env:
        return Path(env)
    if IS_WINDOWS:
        base = os.environ.get("PROGRAMDATA") or os.path.expanduser("~")
        return Path(base) / "Escudo"
    if os.geteuid() == 0:
        return Path("/var/lib/escudo")
    return Path.home() / ".escudo"


@dataclass
class Config:
    data_dir: Path = field(default_factory=default_data_dir)
    # Escaneo
    max_file_size_mb: int = 100
    heuristics: bool = True
    entropy_threshold: float = 7.6
    exclude_dirs: list[str] = field(
        default_factory=lambda: ["/proc", "/sys", "/dev", "/run"]
        if not IS_WINDOWS
        else [r"C:\Windows\WinSxS"]
    )
    # Protección en tiempo real
    watch_paths: list[str] = field(
        default_factory=lambda: [str(Path.home() / "Downloads"), str(Path.home())]
    )
    auto_quarantine: bool = True
    # Prevención de intrusiones
    bruteforce_threshold: int = 5          # fallos de login
    bruteforce_window_s: int = 300         # en esta ventana de tiempo
    portscan_threshold: int = 15           # puertos distintos tocados por una IP
    portscan_window_s: int = 60
    block_duration_s: int = 3600           # 0 = permanente
    whitelist_ips: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])
    blocked_ports_inbound: list[int] = field(default_factory=list)
    dry_run_firewall: bool = False

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    @property
    def signatures_file(self) -> Path:
        return self.data_dir / "signatures.json"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "escudo.log"

    @property
    def blocklist_file(self) -> Path:
        return self.data_dir / "blocked_ips.json"

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        cfg = cls()
        candidate = Path(path) if path else cfg.data_dir / "config.json"
        if candidate.is_file():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            for key, value in data.items():
                if not hasattr(cfg, key):
                    raise ValueError(f"Opción de configuración desconocida: {key}")
                setattr(cfg, key, Path(value) if key == "data_dir" else value)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    def to_json(self) -> str:
        d = asdict(self)
        d["data_dir"] = str(self.data_dir)
        return json.dumps(d, indent=2, ensure_ascii=False)
