"""Modelos: búsqueda en Hugging Face, descarga (reanudable) y estimación de si caben."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
from pathlib import Path

HF = "https://huggingface.co"
MODELS_DIR = Path(os.environ.get("TISMA_MODELS_DIR",
                                 Path.home() / ".local/share/tisma-code/models"))
_ID = re.compile(r"^[\w.-]+/[\w.-]+$")
_FILE = re.compile(r"^[\w./ -]+\.gguf$")
_SPLIT = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")


def _get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "tisma-code/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.load(r)


# ---------------------------------------------------------------- hardware
def hardware() -> dict:
    """RAM y VRAM disponibles (usa el perfil de centinela si existe)."""
    ram_mb = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            ram_mb = int(line.split()[1]) // 1024
    gpus = []
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.strip().splitlines():
            name, mem = (x.strip() for x in line.rsplit(",", 1))
            gpus.append({"name": name, "vram_mb": int(mem)})
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        f = card / "device/mem_info_vram_total"
        if f.exists():
            mb = int(f.read_text()) // (1 << 20)
            # APU: la «VRAM» es una reserva de la RAM compartida, no memoria propia más rápida
            if mb > 2048:
                gpus.append({"name": card.name, "vram_mb": mb})
    bw = None
    try:
        b = json.loads(Path("/var/lib/centinela/bench.json").read_text())
        bw = b["memory"]["bandwidth_gbs"]["read"]
    except (OSError, KeyError, ValueError):
        pass
    return {"ram_mb": ram_mb, "gpus": gpus, "vram_mb": max((g["vram_mb"] for g in gpus), default=0),
            "ram_bandwidth_gbs": bw}


def fit(size_bytes: int, hw: dict, context_overhead: float = 1.2) -> dict:
    """¿Cabe el modelo? Pesos + ~20 % para contexto (KV cache) y búferes."""
    need_mb = size_bytes / (1 << 20) * context_overhead
    vram, ram = hw.get("vram_mb", 0), hw.get("ram_mb", 0)
    if vram and need_mb <= vram * 0.92:
        where, label = "gpu", "Cabe entero en la GPU (rápido)"
    elif vram and need_mb <= vram * 0.92 + ram * 0.6:
        where, label = "mixto", "GPU + RAM (velocidad media)"
    elif need_mb <= ram * 0.85:
        where, label = "cpu", "Solo CPU/RAM (lento)"
    else:
        where, label = "no", "No cabe en este equipo"
    est = None
    if where in ("cpu", "mixto") and hw.get("ram_bandwidth_gbs"):
        # La generación está limitada por leer todos los pesos en cada token
        est = round(hw["ram_bandwidth_gbs"] / (size_bytes / 1e9), 1)
    return {"where": where, "label": label, "need_mb": round(need_mb), "tokens_s_estimate": est}


# ---------------------------------------------------------------- búsqueda
def search(query: str, limit: int = 25) -> list[dict]:
    q = urllib.parse.urlencode({"search": query, "filter": "gguf", "sort": "downloads",
                                "direction": "-1", "limit": str(limit)})
    data = _get_json(f"{HF}/api/models?{q}")
    return [{"id": m["id"], "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
             "updated": (m.get("lastModified") or "")[:10]} for m in data]


def files(model_id: str, hw: dict | None = None) -> list[dict]:
    if not _ID.match(model_id):
        raise ValueError("Identificador de modelo inválido")
    info = _get_json(f"{HF}/api/models/{model_id}?blobs=true")
    sizes: dict[str, int] = {s["rfilename"]: s.get("size") or 0
                             for s in info.get("siblings", []) if s["rfilename"].endswith(".gguf")}
    hw = hw or hardware()
    out = []
    for name, size in sorted(sizes.items()):
        m = _SPLIT.search(name)
        if m and m.group(1) != "00001":
            continue  # las partes 2..N se agrupan con la primera
        total = size
        if m:
            prefix = name[: m.start()]
            total = sum(s for n, s in sizes.items() if n.startswith(prefix) and _SPLIT.search(n))
        if "mmproj" in name.lower():
            continue
        out.append({"file": name, "size": total, "parts": int(m.group(2)) if m else 1, "fit": fit(total, hw)})
    return out


# ---------------------------------------------------------------- descargas
class Downloads:
    def __init__(self, on_progress=None):
        self.state: dict[str, dict] = {}
        self.on_progress = on_progress or (lambda d: None)
        self._lock = threading.Lock()

    def start(self, model_id: str, file: str) -> str:
        if not _ID.match(model_id) or not _FILE.match(file) or ".." in file:
            raise ValueError("Modelo o archivo inválido")
        key = f"{model_id}/{file}"
        with self._lock:
            if self.state.get(key, {}).get("status") == "descargando":
                return key
            self.state[key] = {"key": key, "status": "descargando", "done": 0, "total": 0}
        threading.Thread(target=self._run, args=(model_id, file, key), daemon=True).start()
        return key

    def _parts(self, file: str) -> list[str]:
        m = _SPLIT.search(file)
        if not m:
            return [file]
        n = int(m.group(2))
        prefix = file[: m.start()]
        return [f"{prefix}-{i:05d}-of-{n:05d}.gguf" for i in range(1, n + 1)]

    def _run(self, model_id: str, file: str, key: str) -> None:
        st = self.state[key]
        dest_dir = MODELS_DIR / model_id.replace("/", "__")
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            for part in self._parts(file):
                self._fetch(model_id, part, dest_dir / Path(part).name, st)
            st["status"] = "completo"
            st["path"] = str(dest_dir / Path(file).name)
        except Exception as e:  # noqa: BLE001
            st["status"] = "error"
            st["error"] = str(e)
        self.on_progress(dict(st))

    def _fetch(self, model_id: str, part: str, dest: Path, st: dict) -> None:
        url = f"{HF}/{model_id}/resolve/main/{urllib.parse.quote(part)}"
        tmp = dest.with_suffix(dest.suffix + ".part")
        have = tmp.stat().st_size if tmp.exists() else 0
        if dest.exists():
            return
        req = urllib.request.Request(url, headers={"User-Agent": "tisma-code/0.1",
                                                   **({"Range": f"bytes={have}-"} if have else {})})
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            if have and r.status != 206:
                have = 0  # el servidor no admite reanudar
            total = have + int(r.headers.get("Content-Length", 0))
            st["total"] += total
            st["done"] += have
            last = 0
            with open(tmp, "ab" if have else "wb") as fh:
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
                    st["done"] += len(chunk)
                    if st["done"] - last > 64 << 20:
                        last = st["done"]
                        self.on_progress(dict(st))
        tmp.rename(dest)


def local_models() -> list[dict]:
    out = []
    for p in sorted(MODELS_DIR.glob("*/*.gguf")):
        m = _SPLIT.search(p.name)
        if m and m.group(1) != "00001":
            continue
        out.append({"path": str(p), "name": f"{p.parent.name.replace('__', '/')}/{p.name}",
                    "size": p.stat().st_size})
    return out


def endpoint_models(endpoint: str) -> list[str]:
    try:
        data = _get_json(endpoint.rstrip("/") + "/models", timeout=3)
        return [m["id"] for m in data.get("data", [])]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------- servidor llama.cpp
def port_owner_is_me(port: int) -> bool:
    """¿El proceso que escucha en 127.0.0.1:port es de este usuario? (evita que otro usuario local
    suplante al motor de IA y le dicte llamadas a herramientas al agente)."""
    import os as _os
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in Path(table).read_text().splitlines()[1:]:
                f = line.split()
                if f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                    inodes.add(f[9])
        except OSError:
            continue
    if not inodes:
        return False
    for pid in _os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if _os.stat(f"/proc/{pid}").st_uid != _os.getuid():
                continue
            for fd in _os.listdir(f"/proc/{pid}/fd"):
                link = _os.readlink(f"/proc/{pid}/fd/{fd}")
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    return True
        except OSError:
            continue
    return False


class LocalServer:
    """Sirve un GGUF descargado con llama-server (llama.cpp): puerto aleatorio y clave aleatoria."""

    def __init__(self, port: int = 0):
        self.port = port
        self.api_key = secrets.token_urlsafe(24)
        self.proc: subprocess.Popen | None = None
        self.model: str | None = None

    def start(self, path: str, hw: dict) -> str:
        exe = shutil.which("llama-server")
        if not exe:
            raise RuntimeError("llama-server (llama.cpp) no está instalado")
        p = Path(path).resolve()
        if MODELS_DIR.resolve() not in p.parents or p.suffix != ".gguf":
            raise ValueError("Solo se pueden servir modelos descargados por TISMA Code")
        self.stop()
        f = fit(p.stat().st_size, hw)
        ngl = "999" if f["where"] == "gpu" else "20" if f["where"] == "mixto" else "0"
        if not self.port:
            import socket
            with socket.socket() as sk:
                sk.bind(("127.0.0.1", 0))
                self.port = sk.getsockname()[1]
        self.proc = subprocess.Popen([exe, "-m", str(p), "--host", "127.0.0.1", "--port", str(self.port),
                                      "--api-key", self.api_key, "-ngl", ngl, "-c", "16384", "--jinja"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.model = p.name
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
