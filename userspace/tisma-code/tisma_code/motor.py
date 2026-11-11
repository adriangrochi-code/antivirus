"""Motor TISMA: la misma IA en cualquier equipo, con el motor y la configuración que más rinden en cada uno.

TISMA Code ve siempre lo mismo (una API OpenAI local). Por debajo:

1. **Elección del motor.** Strata si el equipo puede con él (GPU dedicada y ~32 GB entre RAM y VRAM: reparte
   los expertos entre GPU, RAM y SSD). Si no, llama.cpp (CPU multivariante + Vulkan, compilado en la ISO).
2. **Plan según el modelo y el equipo** (leyendo la cabecera GGUF: capas, expertos, KV):
   * todo en la GPU si cabe;
   * modelos MoE con GPU pequeña: atención y capas densas en la GPU, expertos en RAM (`--n-cpu-moe`), la idea
     de Strata aplicada con llama.cpp;
   * modelos densos que no caben: capas repartidas;
   * sin GPU dedicada: CPU (con la gráfica integrada como alternativa que se mide);
   * contexto: el mayor que cabe en la memoria que queda (caché KV en q8_0), hasta lo que pidas.
3. **Medición.** La primera vez (o al pulsar «optimizar») se prueban pocas configuraciones (dispositivo,
   reparto de expertos, hilos, modelo borrador para decodificación especulativa) con un prompt parecido al
   trabajo real del agente y se guarda la más rápida para ese modelo y ese equipo.
4. **Elección del modelo.** Entre los descargados, el de más calidad (más parámetros) que vaya a una velocidad
   usable; con modelos MoE, lo que cuenta para la velocidad son los parámetros ACTIVOS por token.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import time
from pathlib import Path

from . import models

MEDIDAS = models.MODELS_DIR.parent / "motor.json"
KV_BYTES = 1.0625                 # q8_0: 34 bytes por bloque de 32
SO_MB = 2500                      # escritorio, navegador y TISMA Code fuera del modelo
VELOCIDAD_USABLE = 4.0            # tokens/s por debajo de los cuales el agente se hace pesado
STRATA_APP = Path("/opt/strata/Strata.AppImage")


# ---------------------------------------------------------------- GGUF
_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


class _R:
    def __init__(self, f):
        self.f = f

    def take(self, fmt: str):
        n = struct.calcsize(fmt)
        b = self.f.read(n)
        if len(b) != n:
            raise ValueError("GGUF truncado")
        return struct.unpack(fmt, b)[0]

    def string(self, limit: int = 1 << 20) -> str:
        n = self.take("<Q")
        if n > limit:
            raise ValueError("GGUF: cadena demasiado larga")
        return self.f.read(n).decode("utf-8", "replace")

    def value(self, t: int, keep: bool = True):
        if t in _SCALAR:
            return self.take(_SCALAR[t])
        if t == 8:
            return self.string()
        if t == 9:
            et, n = self.take("<I"), self.take("<Q")
            if n > 1 << 24:
                raise ValueError("GGUF: lista demasiado larga")
            if et in _SCALAR:                         # se salta sin leer elemento a elemento
                self.f.seek(n * struct.calcsize(_SCALAR[et]), 1)
                return n
            for _ in range(n):
                self.value(et, False)
            return n                                  # de las listas solo interesa la longitud (vocabulario)
        raise ValueError(f"GGUF: tipo desconocido {t}")


def gguf_info(path: str | Path) -> dict:
    """Metadatos que importan para planificar, y el tamaño de los tensores de expertos frente al resto."""
    p = Path(path)
    with open(p, "rb") as f:
        r = _R(f)
        if f.read(4) != b"GGUF":
            raise ValueError("No es un archivo GGUF")
        version = r.take("<I")
        if version < 2:
            raise ValueError("GGUF demasiado antiguo")
        n_tensors, n_kv = r.take("<Q"), r.take("<Q")
        if n_tensors > 1 << 20 or n_kv > 1 << 16:
            raise ValueError("GGUF: cabecera inválida")
        kv = {}
        for _ in range(n_kv):
            k = r.string(1 << 16)
            kv[k] = r.value(r.take("<I"))
        tensors = []
        for _ in range(n_tensors):
            name = r.string(1 << 16)
            nd = r.take("<I")
            if nd > 8:
                raise ValueError("GGUF: tensor inválido")
            f.seek(8 * nd + 4, 1)                      # dimensiones y tipo
            tensors.append((r.take("<Q"), name))
        align = int(kv.get("general.alignment", 32) or 32)
        start = (f.tell() + align - 1) // align * align
        end = p.stat().st_size
    arch = kv.get("general.architecture", "")
    g = lambda k, d=0: kv.get(f"{arch}.{k}", d)      # noqa: E731
    tensors.sort()
    expert = other = 0
    for i, (off, name) in enumerate(tensors):
        size = (tensors[i + 1][0] if i + 1 < len(tensors) else end - start) - off
        if "_exps." in name:                          # blk.N.ffn_{gate,up,down}_exps.weight (no los compartidos)
            expert += size
        else:
            other += size
    heads = int(g("attention.head_count", 0) or 0)
    return {"arquitectura": arch, "capas": int(g("block_count", 0) or 0),
            "expertos": int(g("expert_count", 0) or 0), "expertos_activos": int(g("expert_used_count", 0) or 0),
            "contexto_max": int(g("context_length", 0) or 0),
            "kv_heads": int(g("attention.head_count_kv", heads) or heads),
            "key_len": int(g("attention.key_length", 0) or (int(g("embedding_length", 0)) // heads if heads else 0)),
            "value_len": int(g("attention.value_length", 0) or 0),
            "tokenizador": kv.get("tokenizer.ggml.model", ""), "vocabulario": kv.get("tokenizer.ggml.tokens", 0),
            "bytes_expertos": expert, "bytes_resto": other, "nombre": kv.get("general.name", p.stem)}


def total_size(path: str | Path) -> int:
    p = Path(path)
    m = models._SPLIT.search(p.name)
    if not m:
        return p.stat().st_size
    prefix = p.name[: m.start()]
    return sum(q.stat().st_size for q in p.parent.glob(prefix + "-*-of-*.gguf"))


# ---------------------------------------------------------------- plan
def kv_mb(info: dict, ctx: int) -> float:
    per_tok = info["capas"] * info["kv_heads"] * (info["key_len"] + (info["value_len"] or info["key_len"])) * KV_BYTES
    return per_tok * ctx / (1 << 20)


def active_bytes(info: dict, size: int) -> float:
    """Bytes que hay que leer por token: con MoE solo los expertos activos (lo que hace rápidos a los MoE)."""
    total = info["bytes_expertos"] + info["bytes_resto"] or size
    scale = size / total if total else 1.0                        # GGUF partido: se extrapola
    if info["expertos"] and info["expertos_activos"] and info["bytes_expertos"]:
        return (info["bytes_resto"] + info["bytes_expertos"] * info["expertos_activos"] / info["expertos"]) * scale
    return float(size)


def has_igpu() -> bool:
    return any(Path("/dev/dri").glob("renderD*"))


def plan(info: dict, size: int, hw: dict, ctx_wanted: int = 32768) -> dict:
    """Configuración inicial razonable (sin medir). Devuelve argumentos extra de llama-server y su motivo."""
    w_mb = size / (1 << 20)
    vram = hw.get("vram_mb", 0)
    budget_ram = hw.get("ram_mb", 0) * 0.92 - SO_MB
    cmax = info.get("contexto_max") or ctx_wanted
    ctx = max(4096, min(ctx_wanted, cmax))

    def fits_ctx(free_mb: float) -> int:
        c = ctx
        while c > 4096 and kv_mb(info, c) > free_mb:
            c //= 2
        return c

    if vram:
        gpu_budget = vram * 0.90 - 400
        c = fits_ctx(gpu_budget - w_mb)
        if w_mb + kv_mb(info, c) <= gpu_budget:
            return {"dispositivo": "gpu", "args": ["-ngl", "999"], "ctx": c, "motivo": "el modelo cabe entero en la GPU"}
        if info["expertos"] and info["bytes_expertos"]:
            scale = size / max(1, info["bytes_expertos"] + info["bytes_resto"])
            rest_mb = info["bytes_resto"] * scale / (1 << 20)
            per_layer = info["bytes_expertos"] * scale / (1 << 20) / max(1, info["capas"])
            c = fits_ctx(min(gpu_budget - rest_mb, budget_ram - (w_mb - rest_mb)))
            room = gpu_budget - rest_mb - kv_mb(info, c)
            if room > 0:
                n_cpu = min(info["capas"], max(0, math.ceil(info["capas"] - room / per_layer)))
                return {"dispositivo": "mixto", "args": ["-ngl", "999", "--n-cpu-moe", str(n_cpu)], "ctx": c,
                        "motivo": f"MoE: atención y {info['capas'] - n_cpu} capas de expertos en la GPU, "
                                  f"{n_cpu} en RAM"}
        layers = max(1, info["capas"])
        c = fits_ctx(gpu_budget * 0.25)
        ngl = max(0, min(layers, int(layers * (gpu_budget - kv_mb(info, c)) / w_mb)))
        if ngl:
            return {"dispositivo": "mixto", "args": ["-ngl", str(ngl)], "ctx": c,
                    "motivo": f"{ngl} de {layers} capas en la GPU, el resto en RAM"}
    c = fits_ctx(budget_ram - w_mb)
    return {"dispositivo": "cpu", "args": ["-ngl", "0", "--device", "none"], "ctx": c,
            "motivo": "sin GPU dedicada: CPU (se mide también la gráfica integrada)" if has_igpu() else "CPU"}


def candidates(info: dict, size: int, hw: dict, base: dict, draft: str | None = None) -> list[dict]:
    """Pocas alternativas que vale la pena medir (cada una cuesta cargar el modelo)."""
    out = [dict(base, nombre="plan")]
    if base["dispositivo"] == "cpu" and has_igpu():
        out.append(dict(base, nombre="gráfica integrada", dispositivo="igpu", args=["-ngl", "999"]))
    if "--n-cpu-moe" in base["args"]:
        n = int(base["args"][base["args"].index("--n-cpu-moe") + 1])
        for d in (-2, 2):
            if 0 <= n + d <= info["capas"]:
                out.append(dict(base, nombre=f"expertos en RAM: {n + d}",
                                args=["-ngl", "999", "--n-cpu-moe", str(n + d)]))
    if draft:
        gpu_draft = ["-ngld", "999"] if base["dispositivo"] in ("gpu", "mixto") else []   # el borrador, también en GPU
        out.append(dict(base, nombre="con borrador", args=base["args"] + ["-md", draft, "--draft-max", "16",
                                                                          "--draft-min", "2", *gpu_draft]))
    return out


def compatible_draft(main: str, info: dict, local: list[dict]) -> str | None:
    """Modelo borrador: mismo tokenizador y vocabulario, y al menos 6 veces más pequeño."""
    size = total_size(main)
    best = None
    for m in local:
        if m["path"] == main or m["size"] * 6 > size:
            continue
        try:
            d = gguf_info(m["path"])
        except (OSError, ValueError):
            continue
        if d["tokenizador"] == info["tokenizador"] and d["vocabulario"] == info["vocabulario"] and info["vocabulario"]:
            if best is None or m["size"] > best["size"]:
                best = m
    return best["path"] if best else None


# ---------------------------------------------------------------- medición
BENCH_PROMPT = ("Reescribe esta función en Python con tipos y una docstring, sin cambiar su comportamiento:\n"
                "def media(xs):\n    t = 0\n    n = 0\n    for x in xs:\n        t = t + x\n        n = n + 1\n"
                "    return t / n if n else 0\n") * 4


def hw_key(hw: dict) -> str:
    return f"ram{hw.get('ram_mb', 0) // 1024}-vram{hw.get('vram_mb', 0) // 1024}-" + \
        ",".join(sorted(g.get("name", "") for g in hw.get("gpus", [])))


def saved(model_file: str, hw: dict) -> dict | None:
    m = models._read_json(MEDIDAS).get(Path(model_file).name)
    return m if m and m.get("equipo") == hw_key(hw) else None


def _save(model_file: str, rec: dict) -> None:
    data = models._read_json(MEDIDAS)
    data[Path(model_file).name] = rec
    MEDIDAS.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEDIDAS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(MEDIDAS)


def bench(path: str, hw: dict, args: list[str], ctx: int, threads: tuple[int, int] | None) -> tuple[float, float]:
    """(tokens/s generando, tokens/s leyendo el prompt) con una configuración; 0 si no arranca."""
    srv = models.LocalServer()
    try:
        ep = srv.start(path, hw, ctx=min(ctx, 8192), threads=threads, extra=args)
        base = ep.rsplit("/v1", 1)[0]
        hdr = {"Authorization": f"Bearer {srv.api_key}", "Content-Type": "application/json"}
        for _ in range(480):
            if srv.proc is None or srv.proc.poll() is not None:
                return 0.0, 0.0
            try:
                with models.urllib.request.urlopen(models.urllib.request.Request(base + "/health", headers=hdr),
                                                   timeout=2) as r:
                    if r.status == 200:
                        break
            except (models.urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)
        body = json.dumps({"prompt": BENCH_PROMPT, "n_predict": 96, "cache_prompt": False, "temperature": 0}).encode()
        with models.urllib.request.urlopen(models.urllib.request.Request(base + "/completion", data=body, headers=hdr),
                                           timeout=900) as r:
            tm = json.load(r).get("timings") or {}
        return float(tm.get("predicted_per_second") or 0), float(tm.get("prompt_per_second") or 0)
    except (OSError, ValueError, RuntimeError):
        return 0.0, 0.0
    finally:
        srv.stop()


def thread_options() -> list[tuple[int, int]]:
    sysd = models._read_json(models.HILOS_SISTEMA)
    logical = int(sysd.get("ia_lote") or os.cpu_count() or 1)
    units = int(sysd.get("ia_generacion") or max(1, logical // 2))
    return [(units, logical)] + ([(logical, units)] if units != logical else [])


def optimize(path: str, hw: dict, ctx_wanted: int = 32768, run=bench, on_step=None) -> dict:
    """Mide las alternativas y guarda la mejor para este modelo y este equipo."""
    info = gguf_info(path)
    size = total_size(path)
    base = plan(info, size, hw, ctx_wanted)
    draft = compatible_draft(path, info, models.local_models())
    results = []
    threads = thread_options()
    for c in candidates(info, size, hw, base, draft):
        g, pp = run(path, hw, c["args"], c["ctx"], threads[0])
        results.append({**c, "hilos": list(threads[0]), "gen": round(g, 2), "prompt": round(pp, 1)})
        if on_step:
            on_step(f"{c['nombre']}: {g:.1f} tokens/s generando, {pp:.0f} leyendo")
    best = max(results, key=lambda r: (r["gen"], r["prompt"]))
    if best["gen"] > 0 and len(threads) > 1 and best["dispositivo"] in ("cpu", "mixto"):
        g, pp = run(path, hw, best["args"], best["ctx"], threads[1])
        if on_step:
            on_step(f"{best['nombre']} con {threads[1][0]} hilos: {g:.1f} tokens/s")
        if g > best["gen"]:
            best = {**best, "hilos": list(threads[1]), "gen": round(g, 2), "prompt": round(pp, 1)}
    rec = {"equipo": hw_key(hw), "args": best["args"], "ctx": best["ctx"], "hilos": best["hilos"],
           "dispositivo": best["dispositivo"], "nombre": best["nombre"], "gen": best["gen"], "prompt": best["prompt"],
           "motivo": best["motivo"], "pruebas": [{k: r[k] for k in ("nombre", "gen", "prompt")} for r in results],
           "fecha": time.strftime("%Y-%m-%d %H:%M")}
    if best["gen"] > 0:
        _save(path, rec)
    return rec


def config_for(path: str, hw: dict, ctx_wanted: int = 32768) -> dict:
    """Lo medido si existe para este equipo; si no, el plan."""
    m = saved(path, hw)
    if m:
        return {"args": m["args"], "ctx": min(ctx_wanted, m["ctx"]) if ctx_wanted else m["ctx"],
                "hilos": tuple(m["hilos"]), "medido": True, "motivo": m["motivo"], "gen": m["gen"]}
    try:
        info = gguf_info(path)
        p = plan(info, total_size(path), hw, ctx_wanted)
        return {"args": p["args"], "ctx": p["ctx"], "hilos": None, "medido": False, "motivo": p["motivo"]}
    except (OSError, ValueError):
        return {"args": [], "ctx": 0, "hilos": None, "medido": False, "motivo": "sin metadatos GGUF"}


# ---------------------------------------------------------------- elección
def estimate_speed(path: str, hw: dict) -> float | None:
    m = saved(path, hw)
    if m:
        return m["gen"]
    try:
        info = gguf_info(path)
    except (OSError, ValueError):
        return None
    size = total_size(path)
    bw = hw.get("ram_bandwidth_gbs") or 20.0
    if hw.get("vram_mb") and size / (1 << 20) < hw["vram_mb"] * 0.85:
        bw = max(bw, 200.0)                       # en VRAM: un orden de magnitud más
    return round(bw * 1e9 * 0.6 / max(1.0, active_bytes(info, size)), 1)   # ~60 % del ancho de banda teórico


def choose_model(local: list[dict], hw: dict) -> dict | None:
    """El de más calidad (tamaño) que vaya a velocidad usable y quepa; si ninguno es usable, el más rápido."""
    ram_budget = hw.get("ram_mb", 0) * 0.92 - SO_MB + hw.get("vram_mb", 0) * 0.9
    rows = []
    for m in local:
        if m["size"] / (1 << 20) > ram_budget:
            continue
        v = estimate_speed(m["path"], hw)
        if v is not None:
            rows.append((v, m))
    if not rows:
        return None
    usable = [r for r in rows if r[0] >= VELOCIDAD_USABLE]
    v, m = max(usable, key=lambda r: r[1]["size"]) if usable else max(rows, key=lambda r: r[0])
    return {**m, "tokens_s": v}


def strata_installed() -> bool:
    return STRATA_APP.exists() or shutil.which("strata") is not None


def choose_engine(hw: dict, strata_alive: bool) -> dict:
    st = models.strata_status(hw)
    if strata_alive:
        return {"motor": "strata", "motivo": "Strata está en marcha"}
    if st["ok"] and strata_installed():
        return {"motor": "strata", "motivo": "Este equipo puede con Strata: ábrelo (menú → Strata) para usarlo",
                "abrir": True}
    return {"motor": "llama.cpp", "motivo": st["motivo"] or "Strata no está instalado"}


# ---------------------------------------------------------------- terminal
def cli(argv: list[str] | None = None) -> int:
    """tisma-code motor [--optimizar MODELO.gguf]: qué motor y configuración usaría este equipo."""
    import argparse
    ap = argparse.ArgumentParser(prog="tisma-code motor")
    ap.add_argument("--optimizar", metavar="GGUF", help="mide las configuraciones de ese modelo y guarda la mejor")
    a = ap.parse_args(argv)
    hw = models.hardware()
    gpus = ", ".join(g["name"] for g in hw["gpus"]) or "ninguna dedicada" + (" (hay gráfica integrada)" if has_igpu() else "")
    print(f"Equipo: {hw['ram_mb'] // 1024} GB de RAM, GPU: {gpus}")
    eng = choose_engine(hw, bool(models.endpoint_models("http://127.0.0.1:8080/v1")))
    print(f"Motor: {eng['motor']} — {eng['motivo']}")
    if a.optimizar:
        rec = optimize(str(Path(a.optimizar).resolve()), hw, on_step=lambda m: print("  ·", m, flush=True))
        print(f"Mejor: {rec['nombre']} — {rec['gen']} tokens/s, contexto {rec['ctx'] // 1024}k, argumentos: "
              + " ".join(rec["args"]))
        return 0 if rec["gen"] else 1
    local = models.local_models()
    for m in local:
        c = config_for(m["path"], hw)
        print(f"- {Path(m['path']).name} ({m['size'] / GB_:.1f} GB): ~{estimate_speed(m['path'], hw)} tokens/s "
              f"{'(medido)' if c['medido'] else '(estimado)'} · {c['motivo']} · contexto {c['ctx'] // 1024}k")
    rec = choose_model(local, hw)
    print(f"Modelo recomendado: {Path(rec['path']).name}" if rec else "Sin modelos descargados.")
    return 0


GB_ = 1 << 30
