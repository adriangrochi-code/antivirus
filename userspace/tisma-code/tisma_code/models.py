"""Modelos: búsqueda en Hugging Face, descarga (reanudable) y estimación de si caben."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import urllib.error
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
def file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


class Downloads:
    def __init__(self, on_progress=None):
        self.state: dict[str, dict] = {}
        self.on_progress = on_progress or (lambda d: None)
        self._lock = threading.Lock()

    def start(self, model_id: str, file: str, sha256: str = "") -> str:
        """sha256: si se conoce (catálogo de especialistas), el archivo se verifica y se descarta si no coincide."""
        if not _ID.match(model_id) or not _FILE.match(file) or ".." in file:
            raise ValueError("Modelo o archivo inválido")
        key = f"{model_id}/{file}"
        with self._lock:
            if self.state.get(key, {}).get("status") == "descargando":
                return key
            self.state[key] = {"key": key, "status": "descargando", "done": 0, "total": 0}
        threading.Thread(target=self._run, args=(model_id, file, key, sha256), daemon=True).start()
        return key

    def _parts(self, file: str) -> list[str]:
        m = _SPLIT.search(file)
        if not m:
            return [file]
        n = int(m.group(2))
        prefix = file[: m.start()]
        return [f"{prefix}-{i:05d}-of-{n:05d}.gguf" for i in range(1, n + 1)]

    def _run(self, model_id: str, file: str, key: str, sha256: str = "") -> None:
        st = self.state[key]
        dest_dir = MODELS_DIR / model_id.replace("/", "__")
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            for part in self._parts(file):
                self._fetch(model_id, part, dest_dir / Path(part).name, st)
            if sha256:
                st["status"] = "verificando"
                self.on_progress(dict(st))
                got = file_sha256(dest_dir / Path(file).name)
                if got != sha256.lower():
                    (dest_dir / Path(file).name).unlink(missing_ok=True)
                    raise ValueError(f"SHA-256 incorrecto ({got[:12]}…): archivo descartado")
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


def server_context(endpoint: str, api_key: str = "local") -> int | None:
    """Ventana REAL del motor (no la que suponga la interfaz): Strata la da en /health (max_context) y
    llama-server en /props (n_ctx por ranura). Con una ventana supuesta mayor que la real, el reinicio de
    contexto llegaba tarde y el motor rechazaba la petición."""
    base = endpoint.rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    hdr = {"User-Agent": "tisma-code/0.1", "Authorization": f"Bearer {api_key}"}
    for path, keys in (("/health", ("max_context",)), ("/props", ("default_generation_settings", "n_ctx"))):
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, headers=hdr), timeout=3) as r:  # noqa: S310
                v = json.load(r)
            for k in keys:
                v = v[k]
            if isinstance(v, int) and v >= 1024:
                return v
        except Exception:  # noqa: BLE001 (motor apagado, otra API…)
            continue
    return None


def strata_status(hw: dict) -> dict:
    """Strata (motor principal) necesita GPU dedicada (NVIDIA, AMD RX 6000+, Intel Arc o Apple) y unos 32 GB
    entre RAM y VRAM para su modelo más pequeño (Coder); no tiene motor solo-CPU ni para APU. En equipos que
    no llegan, TISMA Code usa llama.cpp con modelos GGUF pequeños (mismo arnés y herramientas)."""
    total = hw.get("ram_mb", 0) + hw.get("vram_mb", 0)
    if not hw.get("gpus"):
        return {"ok": False, "motivo": "Strata necesita una GPU dedicada (este equipo solo tiene gráfica integrada): "
                "se usará llama.cpp con un modelo descargado"}
    if total < 32000:
        return {"ok": False, "motivo": f"Strata necesita ~32 GB entre RAM y VRAM (hay {total // 1024} GB): "
                "se usará llama.cpp con un modelo descargado"}
    return {"ok": True, "motivo": ""}


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


HILOS_SISTEMA = Path("/etc/centinela/hilos.json")       # lo escribe centinela según la topología de la CPU
HILOS_MEDIDOS = MODELS_DIR.parent / "hilos.json"        # mediciones por modelo (usuario)


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def threads_for(model_file: str) -> tuple[int, int] | None:
    """(hilos de generación, hilos de lote) para llama.cpp: la medición de ese modelo si existe; si no, lo que
    centinela dedujo de la topología; si tampoco, None (llama.cpp decide)."""
    m = _read_json(HILOS_MEDIDOS).get(Path(model_file).name)
    if m:
        return int(m["t"]), int(m["tb"])
    sysd = _read_json(HILOS_SISTEMA)
    if sysd.get("ia_generacion") and sysd.get("ia_lote"):
        return int(sysd["ia_generacion"]), int(sysd["ia_lote"])
    return None


BENCH_PROMPT = ("Explica paso a paso cómo funciona un intercambiador de calor de placas, qué variables "
                "determinan su eficiencia y cómo se dimensiona para un caudal dado. ") * 6


def _bench_once(path: str, hw: dict, t: int, tb: int) -> tuple[float, float]:
    """Arranca el modelo con t/tb hilos y devuelve (tokens/s generando, tokens/s procesando el prompt)."""
    srv = LocalServer()
    try:
        ep = srv.start(path, hw, ctx=4096, threads=(t, tb))
        base = ep.rsplit("/v1", 1)[0]
        hdr = {"Authorization": f"Bearer {srv.api_key}", "Content-Type": "application/json"}
        import time as _t
        for _ in range(240):                                    # carga del modelo
            try:
                with urllib.request.urlopen(urllib.request.Request(base + "/health", headers=hdr), timeout=2) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                pass
            _t.sleep(0.5)
        body = json.dumps({"prompt": BENCH_PROMPT, "n_predict": 48, "cache_prompt": False,
                           "temperature": 0}).encode()
        with urllib.request.urlopen(urllib.request.Request(base + "/completion", data=body, headers=hdr),
                                    timeout=600) as r:
            tm = json.load(r).get("timings") or {}
        return float(tm.get("predicted_per_second") or 0), float(tm.get("prompt_per_second") or 0)
    finally:
        srv.stop()


def measure_threads(path: str, hw: dict, bench=None) -> dict:
    """Mide qué número de hilos va mejor para este modelo en este equipo (dos ejecuciones cortas) y lo guarda.
    Candidatos: un hilo por unidad de ejecución (núcleo físico o módulo CMT) o todos los núcleos lógicos."""
    bench = bench or (lambda t, tb: _bench_once(path, hw, t, tb))
    sysd = _read_json(HILOS_SISTEMA)
    logical = int(sysd.get("ia_lote") or os.cpu_count() or 1)
    units = int(sysd.get("ia_generacion") or max(1, logical // 2))
    if units == logical:
        res = {"t": units, "tb": logical, "medido": False}
    else:
        g1, p1 = bench(units, logical)          # generación con «units», prompt con «logical»
        g2, p2 = bench(logical, units)          # y al revés
        res = {"t": units if g1 >= g2 else logical, "tb": logical if p1 >= p2 else units, "medido": True,
               "datos": {f"generacion_{units}": round(g1, 2), f"generacion_{logical}": round(g2, 2),
                         f"prompt_{logical}": round(p1, 2), f"prompt_{units}": round(p2, 2)}}
    allm = _read_json(HILOS_MEDIDOS)
    allm[Path(path).name] = res
    HILOS_MEDIDOS.parent.mkdir(parents=True, exist_ok=True)
    HILOS_MEDIDOS.write_text(json.dumps(allm, indent=2))
    return res


# Caché KV (el «contexto» del modelo) en 8 bits en vez de 16: la mitad de memoria para la misma conversación,
# con pérdida de calidad inapreciable. La V comprimida exige Flash Attention («auto» lo activa si el backend
# lo soporta; si no, LocalServer relanza sin compresión).
KV_QUANT = ["-fa", "auto", "-ctk", "q8_0", "-ctv", "q8_0"]


class LocalServer:
    """Sirve un GGUF descargado con llama-server (llama.cpp): puerto aleatorio y clave aleatoria."""

    def __init__(self, port: int = 0):
        self.port = port
        self.api_key = secrets.token_urlsafe(24)
        self.proc: subprocess.Popen | None = None
        self.model: str | None = None
        self.ctx = 0

    def start(self, path: str, hw: dict, ctx: int = 0, threads: tuple[int, int] | None = None) -> str:
        exe = shutil.which("llama-server")
        if not exe:
            raise RuntimeError("llama-server (llama.cpp) no está instalado")
        p = Path(path).resolve()
        if MODELS_DIR.resolve() not in p.parents or p.suffix != ".gguf":
            raise ValueError("Solo se pueden servir modelos descargados por TISMA Code")
        self.stop()
        # 32k con 16 GB o más (con la caché KV en q8_0, un modelo de 7-8 B ocupa ~1 GB de KV a 32k)
        ctx = ctx or (32768 if hw.get("ram_mb", 0) >= 15000 else 16384)
        self.ctx = ctx
        f = fit(p.stat().st_size, hw)
        ngl = "999" if f["where"] == "gpu" else "20" if f["where"] == "mixto" else "0"
        if not self.port:
            import socket
            with socket.socket() as sk:
                sk.bind(("127.0.0.1", 0))
                self.port = sk.getsockname()[1]
        th = threads or threads_for(str(p))
        thr = ["-t", str(th[0]), "-tb", str(th[1])] if th else []
        base = [exe, "-m", str(p), "--host", "127.0.0.1", "--port", str(self.port),
                "--api-key", self.api_key, "-ngl", ngl, "-c", str(ctx), "--jinja",
                # reutiliza trozos de la caché KV aunque el prefijo cambie (p. ej. tras un reinicio de contexto)
                "--cache-reuse", "256", *thr]
        self.kv_quant = True
        self.proc = self._spawn(base + KV_QUANT)
        self.model = p.name
        threading.Thread(target=self._fallback, args=(self.proc, base), daemon=True).start()
        return f"http://127.0.0.1:{self.port}/v1"

    @staticmethod
    def _spawn(argv: list[str]) -> subprocess.Popen:
        return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _fallback(self, proc: subprocess.Popen, base: list[str], wait_s: float = 180) -> None:
        """Si el motor no admite la caché KV comprimida (requiere Flash Attention), se relanza sin ella."""
        try:
            rc = proc.wait(wait_s)
        except subprocess.TimeoutExpired:
            return                                   # sigue vivo: funcionó
        if rc != 0 and self.proc is proc and self.kv_quant:
            self.kv_quant = False
            self.proc = self._spawn(base)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
