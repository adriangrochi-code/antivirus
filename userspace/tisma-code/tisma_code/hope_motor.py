"""La red de Hope como un motor más de TISMA Code (contrato acordado con Hope, sin código de Hope).

Hope no es GGUF (trabaja por bytes, con cuantización propia), así que llama.cpp no lo corre. Junto a cada
checkpoint viene ``hope_modelo.json`` y Hope lo sirve con su propio servidor, que habla la misma API OpenAI
local que el resto de motores:

    python3 -m hope serve --host 127.0.0.1 --port N --api-key K --modelo DIR --dispositivo D --contexto-bytes C

Aquí: validar esos metadatos (vienen con el modelo: no se confía en ellos a ciegas), elegir el dispositivo del
equipo (CUDA, XPU de Intel Arc o CPU), el contexto que cabe y una velocidad estimada, y lanzar el servidor en
la misma jaula de bubblewrap que llama.cpp. Hope solo se elige solo si la medición dice que gana EN ESTE equipo;
mientras tanto se usa si el usuario lo elige a mano.

Hope es privado: este módulo no lo incluye. Sin Hope instalado, simplemente no hay modelos Hope.
"""

from __future__ import annotations

import json
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

from . import models

HOPE_DIR = models.MODELS_DIR / "hope"
META = "hope_modelo.json"
ESQUEMA = 1
VENDOR = Path("/usr/lib/centinela/python/vendor")
DISPOSITIVOS = ("cuda", "xpu", "cpu")
SO_MB = 2500
VELOCIDAD_USABLE = 4.0

# Intel Arc (gráficas dedicadas): Alchemist (DG2, 0x56xx) y Battlemage (0xe20x)
_ARC = ("0x56", "0xe20")


class MetaError(ValueError):
    pass


def _num(d: dict, k: str, lo: float, hi: float, entero: bool = False):
    v = d.get(k)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
        raise MetaError(f"{k}: valor inválido o fuera de rango ({v!r})")
    if entero and int(v) != v:
        raise MetaError(f"{k}: debe ser entero")
    return int(v) if entero else float(v)


def leer_meta(carpeta: str | Path) -> dict:
    """Lee y valida hope_modelo.json. Lanza MetaError si falta algo o hay valores absurdos."""
    p = Path(carpeta) / META
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MetaError(f"no se pudo leer {META}: {e}") from e
    if not isinstance(raw, dict):
        raise MetaError(f"{META} debe ser un objeto")
    if raw.get("version_esquema") != ESQUEMA:
        raise MetaError(f"versión de esquema {raw.get('version_esquema')!r} no soportada (se espera {ESQUEMA})")
    m = {
        "preset": str(raw.get("preset") or "")[:64],
        "parametros_totales": _num(raw, "parametros_totales", 1e5, 1e13, True),
        "activos_por_byte": _num(raw, "activos_por_byte", 1e5, 1e13, True),
        "flops_por_byte": _num(raw, "flops_por_byte", 0, 1e15) if "flops_por_byte" in raw else None,
        "bytes_por_token_equiv": _num(raw, "bytes_por_token_equiv", 1, 16) if "bytes_por_token_equiv" in raw else 4.0,
        "kv_bytes_por_byte_contexto": _num(raw, "kv_bytes_por_byte_contexto", 0, 1e7),
        "contexto_max_bytes": _num(raw, "contexto_max_bytes", 1024, 1 << 34, True),
        "cuantizacion": _num(raw, "cuantizacion", 2, 32, True),
        "hash": str(raw.get("hash") or "")[:128],
        "linaje": str(raw.get("linaje") or "")[:256],
        "licencia_pesos": str(raw.get("licencia_pesos") or "desconocida")[:64],
    }
    if m["activos_por_byte"] > m["parametros_totales"]:
        raise MetaError("activos_por_byte no puede superar a parametros_totales")
    disp = []
    for d in raw.get("dispositivos") or []:
        if not isinstance(d, dict) or d.get("tipo") not in DISPOSITIVOS:
            continue
        tok = d.get("tok_s_medido")
        disp.append({"tipo": d["tipo"],
                     "vram_min_gb": _num(d, "vram_min_gb", 0, 1024) if d.get("vram_min_gb") is not None else 0.0,
                     "tok_s_medido": float(tok) if isinstance(tok, (int, float)) and 0 < tok < 1e5 else None})
    if not disp:
        raise MetaError("el modelo no declara ningún dispositivo soportado (cuda, xpu o cpu)")
    m["dispositivos"] = disp
    return m


def modelos_locales(base: Path | None = None) -> list[dict]:
    """Checkpoints de Hope con metadatos válidos (los inválidos se listan con su error, sin usarse)."""
    base = base or HOPE_DIR
    out = []
    for meta in sorted(base.glob(f"*/{META}")):
        d = meta.parent
        try:
            out.append({"path": str(d), "name": f"hope/{d.name}", "meta": leer_meta(d)})
        except MetaError as e:
            out.append({"path": str(d), "name": f"hope/{d.name}", "error": str(e)})
    return out


# ---------------------------------------------------------------- equipo
def intel_arc(sys_drm: Path = Path("/sys/class/drm")) -> bool:
    for card in sys_drm.glob("card[0-9]"):
        try:
            if (card / "device/vendor").read_text().strip() == "0x8086" and \
                    (card / "device/device").read_text().strip().lower().startswith(_ARC):
                return True
        except OSError:
            continue
    return False


def dispositivos_equipo(hw: dict, arc: bool | None = None) -> dict[str, float | None]:
    """{tipo: VRAM en GB (None si no se sabe)} de lo que este equipo puede usar para Hope."""
    out: dict[str, float | None] = {"cpu": None}
    nv = [g for g in hw.get("gpus", []) if not str(g.get("name", "")).startswith("card")]   # de nvidia-smi
    if nv:
        out["cuda"] = max(g["vram_mb"] for g in nv) / 1024
    if intel_arc() if arc is None else arc:
        out["xpu"] = None             # i915/xe no publican la VRAM en sysfs de forma fiable
    return out


def bytes_por_parametro(bits: int) -> float:
    return bits / 8


def plan(meta: dict, hw: dict, ctx_bytes_wanted: int = 131072, arc: bool | None = None) -> dict | None:
    """Dispositivo (el primero que sirva en el orden CUDA → XPU → CPU) y contexto en bytes que cabe."""
    peso_mb = meta["parametros_totales"] * bytes_por_parametro(meta["cuantizacion"]) / (1 << 20)
    equipo = dispositivos_equipo(hw, arc)
    for d in sorted(meta["dispositivos"], key=lambda x: DISPOSITIVOS.index(x["tipo"])):
        if d["tipo"] not in equipo:
            continue
        vram_gb = equipo[d["tipo"]]
        if d["tipo"] == "cpu":
            libre = hw.get("ram_mb", 0) * 0.92 - SO_MB - peso_mb
        else:
            if vram_gb is not None and vram_gb < d["vram_min_gb"]:
                continue
            libre = (vram_gb * 1024 * 0.90 - 400 - peso_mb) if vram_gb is not None else 2048.0
        if libre <= 0:
            continue
        ctx = min(ctx_bytes_wanted, meta["contexto_max_bytes"])
        kv = meta["kv_bytes_por_byte_contexto"]
        while ctx > 4096 and kv * ctx / (1 << 20) > libre:
            ctx //= 2
        return {"dispositivo": d["tipo"], "contexto_bytes": int(ctx), "tok_s_medido": d["tok_s_medido"],
                "motivo": f"Hope en {d['tipo'].upper()} con {ctx // 1024} KB de contexto"}
    return None


def estimar_velocidad(meta: dict, p: dict, hw: dict) -> float:
    """Tokens equivalentes por segundo: lo medido por Hope en ese dispositivo, o una cota por ancho de banda
    (cada byte lee los parámetros activos; un token son ~bytes_por_token_equiv bytes)."""
    if p.get("tok_s_medido"):
        return float(p["tok_s_medido"])
    bw = 200.0 if p["dispositivo"] in ("cuda", "xpu") else (hw.get("ram_bandwidth_gbs") or 20.0)
    bytes_s = bw * 1e9 * 0.6 / max(1.0, meta["activos_por_byte"] * bytes_por_parametro(meta["cuantizacion"]))
    return round(bytes_s / meta["bytes_por_token_equiv"], 1)


def instalado(vendor: Path = VENDOR) -> bool:
    return (vendor / "hope").is_dir()


# ---------------------------------------------------------------- servidor
class HopeServer:
    """Lanza «python3 -m hope serve» en la jaula del motor: puerto y clave aleatorios, solo 127.0.0.1."""

    def __init__(self):
        self.port = 0
        self.api_key = secrets.token_urlsafe(24)
        self.proc: subprocess.Popen | None = None
        self.plan: dict = {}

    def argv(self, carpeta: str, p: dict) -> list[str]:
        d = Path(carpeta).resolve()
        if HOPE_DIR.resolve() not in d.parents:
            raise ValueError("Solo se pueden servir modelos de Hope instalados en la carpeta de modelos")
        py = shutil.which("python3") or "/usr/bin/python3"
        # -I: sin PYTHONPATH ni la carpeta actual; solo se añade la carpeta de Hope del sistema
        code = (f"import sys, runpy; sys.path.insert(0, {str(VENDOR)!r}); sys.argv = ['hope'] + sys.argv[1:]; "
                "runpy.run_module('hope', run_name='__main__', alter_sys=True)")
        return [py, "-I", "-c", code, "serve", "--host", "127.0.0.1", "--port", str(self.port),
                "--api-key", self.api_key, "--modelo", str(d), "--dispositivo", p["dispositivo"],
                "--contexto-bytes", str(p["contexto_bytes"])]

    def start(self, carpeta: str, hw: dict) -> str:
        if not instalado():
            raise RuntimeError("Hope no está instalado en este sistema")
        p = plan(leer_meta(carpeta), hw)
        if not p:
            raise RuntimeError("Este equipo no tiene memoria o dispositivo suficientes para ese modelo de Hope")
        self.stop()
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            self.port = sk.getsockname()[1]
        self.plan = p
        self.proc = subprocess.Popen(models.jail_engine(self.argv(carpeta, p)),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

