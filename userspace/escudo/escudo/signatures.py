"""Base de datos de firmas: hashes SHA-256 conocidos y patrones de bytes."""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Cadena de prueba EICAR (estándar de la industria, inofensiva). Se construye
# por partes para que este archivo fuente no sea detectado por otros antivirus.
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
EICAR_SHA256 = "275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"

# Patrones incorporados: indicadores típicos de ataques externos
# (shells inversas, descargadores, mineros, volcado de credenciales...).
BUILTIN_PATTERNS: list[dict] = [
    {"name": "EICAR-Test-File", "regex": re.escape(EICAR.decode()), "severity": "high"},
    {"name": "Linux.ReverseShell.DevTcp", "regex": r"/dev/tcp/\d{1,3}(\.\d{1,3}){3}/\d+", "severity": "high"},
    {"name": "Generic.ReverseShell.Netcat", "regex": r"\bnc(at)?\b[^\n]{0,40}\s-e\s+(/bin/(ba)?sh|cmd(\.exe)?)", "severity": "high"},
    {"name": "Generic.ReverseShell.Python", "regex": r"socket\.socket\([^\n]{0,200}(subprocess|pty\.spawn|os\.dup2)", "severity": "high"},
    {"name": "Win.PowerShell.DownloadCradle", "regex": r"(?i)(IEX|Invoke-Expression)\s*\(?\s*\(?\s*New-Object\s+(System\.)?Net\.WebClient\)?\.Download(String|File)", "severity": "high"},
    {"name": "Win.PowerShell.EncodedHidden", "regex": r"(?i)powershell(\.exe)?[^\n]{0,80}-w(indowstyle)?\s+hidden[^\n]{0,80}-e(nc|ncodedcommand)?\s+[A-Za-z0-9+/=]{40,}", "severity": "medium"},
    {"name": "Win.HackTool.Mimikatz", "regex": r"(?i)(sekurlsa::logonpasswords|Invoke-Mimikatz|gentilkiwi)", "severity": "high"},
    {"name": "Linux.Downloader.CurlPipeShell", "regex": r"(curl|wget)\s[^\n|]{0,200}\|\s*(sudo\s+)?(ba)?sh\b", "severity": "medium"},
    {"name": "Generic.CoinMiner.Stratum", "regex": r"stratum\+(tcp|ssl)://", "severity": "medium"},
    {"name": "Linux.Persistence.CronDownloader", "regex": r"\*\s+\*\s+\*\s+\*\s+\*[^\n]{0,80}(curl|wget)[^\n]{0,200}\|\s*(ba)?sh", "severity": "high"},
    {"name": "Generic.Ransomware.Note", "regex": r"(?i)(your files (have been|are) encrypted|send \d+(\.\d+)? (btc|bitcoin))", "severity": "high"},
    {"name": "Win.Ransomware.ShadowCopyDelete", "regex": r"(?i)vssadmin(\.exe)?\s+delete\s+shadows", "severity": "high"},
    {"name": "Generic.WebShell.PHPEval", "regex": r"(?i)eval\s*\(\s*(base64_decode|gzinflate|str_rot13)\s*\(\s*\$_(POST|GET|REQUEST|COOKIE)", "severity": "high"},
]

_HEX64 = re.compile(r"\b[a-fA-F0-9]{64}\b")


@dataclass
class Pattern:
    name: str
    regex: re.Pattern[bytes]
    severity: str


class SignatureDB:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.hashes: dict[str, str] = {EICAR_SHA256: "EICAR-Test-File"}
        self.custom_patterns: list[dict] = []
        self.patterns: list[Pattern] = []
        if path and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.hashes.update({k.lower(): v for k, v in data.get("hashes", {}).items()})
            self.custom_patterns = data.get("patterns", [])
        self._compile()

    def _compile(self) -> None:
        self.patterns = [
            Pattern(p["name"], re.compile(p["regex"].encode()), p.get("severity", "medium"))
            for p in BUILTIN_PATTERNS + self.custom_patterns
        ]

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"hashes": self.hashes, "patterns": self.custom_patterns}
        self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def lookup_hash(self, sha256: str) -> str | None:
        return self.hashes.get(sha256.lower())

    def add_hash(self, sha256: str, name: str = "Malware.Hash") -> None:
        if not _HEX64.fullmatch(sha256):
            raise ValueError(f"SHA-256 inválido: {sha256}")
        self.hashes[sha256.lower()] = name

    def add_pattern(self, name: str, regex: str, severity: str = "medium") -> None:
        re.compile(regex.encode())  # valida
        self.custom_patterns.append({"name": name, "regex": regex, "severity": severity})
        self._compile()

    def import_hash_list(self, source: str, name: str = "Malware.Feed") -> int:
        """Importa hashes SHA-256 desde un archivo local o URL (uno por línea,
        formatos tipo MalwareBazaar / CSV aceptados: se extrae cualquier hash de 64 hex)."""
        if re.match(r"^https?://", source):
            with urllib.request.urlopen(source, timeout=60) as resp:  # noqa: S310
                text = resp.read().decode("utf-8", errors="replace")
        else:
            text = Path(source).read_text(encoding="utf-8", errors="replace")
        before = len(self.hashes)
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            for h in _HEX64.findall(line):
                self.hashes.setdefault(h.lower(), name)
        return len(self.hashes) - before
