"""Mediciones de rendimiento de cada componente."""

from __future__ import annotations

import glob
import json
import mmap
import os
import random
import re
import shutil
import subprocess
import tempfile
import time

MEMBENCH_PATHS = ["/usr/lib/centinela/centinela-membench",
                  os.path.join(os.path.dirname(__file__), "..", "build", "centinela-membench")]


def _membench_bin() -> str | None:
    for p in MEMBENCH_PATHS:
        if os.access(p, os.X_OK):
            return p
    return shutil.which("centinela-membench")


def memory(quick: bool = False) -> dict:
    exe = _membench_bin()
    if not exe:
        return {"error": "centinela-membench no instalado"}
    out = subprocess.run([exe] + (["--quick"] if quick else []), capture_output=True,
                         text=True, timeout=600, check=True).stdout
    return json.loads(out)


# ---------------------------------------------------------------- compresión
_ZSTD_RX = re.compile(r"\(x?([\d.]+)\),\s*([\d.]+)\s*MB/s[ ,]+([\d.]+)\s*MB/s")


def _sample_file(size_mb: int = 64) -> str:
    """Muestra parecida a la memoria anónima: binarios, texto y páginas vacías."""
    fd, path = tempfile.mkstemp(prefix="centinela-sample-")
    target = size_mb << 20
    written = 0
    sources = sorted(glob.glob("/usr/bin/*"))[:400] + sorted(glob.glob("/usr/share/doc/*/*"))[:400]
    random.Random(42).shuffle(sources)
    with os.fdopen(fd, "wb") as out:
        for s in sources:
            if written >= target:
                break
            try:
                with open(s, "rb") as fh:
                    chunk = fh.read(min(1 << 20, target - written))
            except OSError:
                continue
            out.write(chunk)
            written += len(chunk)
            if random.random() < 0.15:  # páginas a cero, frecuentes en RAM real
                out.write(b"\0" * 65536)
                written += 65536
    return path


def compression() -> dict:
    res: dict = {}
    sample = _sample_file()
    try:
        for tool, args in (("zstd", ["-b1", "-i2"]), ("lz4", ["-b1", "-i2"])):
            exe = shutil.which(tool)
            if not exe:
                continue
            out = subprocess.run([exe, *args, sample], capture_output=True, text=True, timeout=300)
            m = _ZSTD_RX.search(out.stdout + out.stderr)
            if m:
                res[tool] = {"ratio": float(m.group(1)), "compress_mbs": float(m.group(2)),
                             "decompress_mbs": float(m.group(3))}
    finally:
        os.unlink(sample)
    return res


# ---------------------------------------------------------------- almacenamiento
def _direct_read_bench(dev: str, seconds: float = 2.0) -> dict:
    """Lecturas O_DIRECT (sin caché) sobre el dispositivo de bloques: SOLO LECTURA."""
    fd = os.open(dev, os.O_RDONLY | getattr(os, "O_DIRECT", 0))
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        buf4k = mmap.mmap(-1, 4096)
        lat = []
        rng = random.Random(7)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            off = rng.randrange(0, max(size - 4096, 4096) // 4096) * 4096
            t = time.perf_counter()
            os.preadv(fd, [buf4k], off)
            lat.append(time.perf_counter() - t)
        lat.sort()
        buf1m = mmap.mmap(-1, 1 << 20)
        read = 0
        t0 = time.perf_counter()
        off = 0
        while time.perf_counter() - t0 < seconds and off + (1 << 20) <= size:
            read += os.preadv(fd, [buf1m], off)
            off += 1 << 20
        seq = read / (time.perf_counter() - t0) / 1e6
    finally:
        os.close(fd)
    return {
        "rand4k_read_us": round(lat[len(lat) // 2] * 1e6, 1),
        "rand4k_read_p99_us": round(lat[int(len(lat) * 0.99)] * 1e6, 1),
        "rand4k_iops_qd1": round(len(lat) / seconds),
        "seq_read_mbs": round(seq, 1),
    }


def storage(disks: list[dict]) -> dict:
    res = {}
    for d in disks:
        if d.get("size_gb", 0) < 1:
            continue
        try:
            res[d["name"]] = _direct_read_bench(f"/dev/{d['name']}")
        except OSError as e:
            res[d["name"]] = {"error": str(e)}
    return res


def run_all(hw: dict, quick: bool = False, progress=print) -> dict:
    out: dict = {"time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    progress("Midiendo memoria (ancho de banda, latencia, núcleo a núcleo)…")
    try:
        out["memory"] = memory(quick)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        out["memory"] = {"error": str(e)}
    progress("Midiendo compresión (zstd vs lz4) en esta CPU…")
    out["compression"] = compression()
    progress("Midiendo latencia de discos (solo lectura)…")
    out["storage"] = storage(hw.get("storage", []))
    return out


def summarize(b: dict) -> list[str]:
    lines = []
    m = b.get("memory", {})
    if "bandwidth_gbs" in m:
        bw = m["bandwidth_gbs"]
        lines.append(f"RAM: lectura {bw['read']} GB/s, escritura {bw['write']} GB/s, copia {bw['copy']} GB/s "
                     f"(1 hilo: {bw['read_single_thread']} GB/s)")
        lt = m["latency_ns"]
        lines.append("Latencia: " + ", ".join(f"{int(k) // 1024 if int(k) >= 1024 else k}"
                                                f"{'MB' if int(k) >= 1024 else 'KB'} {v:.1f} ns" for k, v in lt.items()))
        c2c = m.get("core_to_core_ns") or []
        vals = [v for row in c2c for v in row if v > 0]
        if vals:
            lines.append(f"Núcleo a núcleo: mín {min(vals):.0f} ns, máx {max(vals):.0f} ns")
    for algo, r in b.get("compression", {}).items():
        lines.append(f"{algo}: ratio {r['ratio']}, comprime {r['compress_mbs']} MB/s, "
                     f"descomprime {r['decompress_mbs']} MB/s")
    for d, r in b.get("storage", {}).items():
        if "error" in r:
            lines.append(f"Disco {d}: {r['error']}")
        else:
            lines.append(f"Disco {d}: 4K aleatorio {r['rand4k_read_us']} µs (p99 {r['rand4k_read_p99_us']} µs), "
                         f"secuencial {r['seq_read_mbs']} MB/s")
    return lines
