"""Perfil de equipo firmado (documento de traspaso, §3): qué ajustar en cada modo y clase de carga, por modelo.

Un perfil es un JSON declarativo para UN modelo (DMI + CPU), firmado con Ed25519 por quien lo midió. Centinela
solo usa perfiles que:
* tienen una firma válida de una clave de confianza (/usr/share/tisma/perfiles/claves/*.pem; la privada nunca
  va al repositorio);
* pasan la validación: solo ajustes de un VOCABULARIO CERRADO, cada uno con sus valores permitidos (nada de rutas
  libres), y límites duros que ningún modo puede cruzar (mitigaciones, temperatura, memoria reservada);
* corresponden a este equipo, y no son más viejos que el último aceptado (anti-retroceso).
Si algo falla, se usa el genérico (los valores de Debian): fallar cerrado.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import sysfs

FORMATO = 1
CLAVES_DIR = Path(os.environ.get("TISMA_PERFIL_CLAVES", "/usr/share/tisma/perfiles/claves"))
MODOS = ("eco", "normal", "maximo")
CLASES = ("inactivo", "interactivo", "multimedia", "lote", "juego", "ia", "fondo")

# Vocabulario cerrado: ajuste → valores permitidos (lista) o rango (min, max). Ninguno toca mitigaciones,
# firmas, Secure Boot ni frecuencias/voltajes por encima de fábrica (límites duros, §5).
AJUSTES: dict[str, list | tuple] = {
    "cpu.governor": ["powersave", "conservative", "schedutil", "ondemand", "performance"],
    "cpu.boost": [0, 1],
    "cpu.epp": ["power", "balance_power", "balance_performance", "performance"],
    "cpu.modulos_activos": (1, 64),              # Excavator: módulos CMT encendidos (el resto, cpuN/online=0)
    "pcie.aspm": ["default", "performance", "powersave", "powersupersave"],
    "sata.alpm": ["max_performance", "medium_power", "med_power_with_dipm", "min_power"],
    "nvme.apst_latencia_us": (0, 100000),
    "gpu.dpm": ["auto", "low", "high"],
    "wifi.ahorro": [0, 1],
    "usb.autosuspend_s": (-1, 60),               # -1 = desactivado
    "audio.power_save_s": (0, 60),
    "vm.dirty_writeback_centisecs": (100, 6000),
    "vm.swappiness": (0, 200),
    "vm.page_cluster": (0, 3),
    "workqueue.power_efficient": [0, 1],
    "zram.algoritmo": ["lz4", "zstd", "lz4 zstd"],
    "zram.tamano_pct": (10, 100),
    "thp": ["never", "madvise"],                 # «always» no: desperdicia memoria en escritorio
    "scx.planificador": ["", "scx_lavd", "scx_bpfland", "scx_rusty"],
    "fondo.peso_cpu": (1, 100),
    "fondo.solo_con_cargador": [0, 1],
    "ia.hilos_por_modulo": (1, 2),
    "ia.kv": ["q8_0", "f16"],
    "ia.contexto_max": (2048, 131072),
    "juego.tope_fps": (0, 240),                  # 0 = sin tope
}
LIMITES = {"temp_max_c": (60, 100), "memoria_reservada_mb": (256, 8192)}


class PerfilInvalido(Exception):
    pass


# ---------------------------------------------------------------- validación (esquema)
def _valor_ok(clave: str, v) -> bool:
    regla = AJUSTES[clave]
    if isinstance(regla, tuple):
        return isinstance(v, int) and not isinstance(v, bool) and regla[0] <= v <= regla[1]
    return v in regla


def validar(p: dict) -> list[str]:
    """Errores del perfil (vacío = válido)."""
    e = []
    if p.get("formato") != FORMATO:
        e.append(f"formato debe ser {FORMATO}")
    if not isinstance(p.get("version"), int) or p["version"] < 1:
        e.append("version: entero ≥ 1 (anti-retroceso)")
    eq = p.get("equipo")
    if not isinstance(eq, dict) or not eq.get("dmi_product_version") and not eq.get("dmi_product_name"):
        e.append("equipo: falta dmi_product_name o dmi_product_version")
    lim = p.get("limites_duros") or {}
    for k, (lo, hi) in LIMITES.items():
        if not isinstance(lim.get(k), int) or not lo <= lim[k] <= hi:
            e.append(f"limites_duros.{k}: entero entre {lo} y {hi}")
    if lim.get("mitigaciones", "auto") != "auto":
        e.append("limites_duros.mitigaciones: siempre «auto» (nunca se desactivan)")
    modos = p.get("modos")
    if not isinstance(modos, dict) or set(modos) - set(MODOS) or not modos:
        e.append(f"modos: solo {', '.join(MODOS)}")
        modos = {}
    agresivo = p.get("agresivo") or {}
    if not isinstance(agresivo, dict) or set(agresivo) - set(MODOS):
        e.append(f"agresivo: solo {', '.join(MODOS)}")
        agresivo = {}
    seguros = set(p.get("agresivo_seguro", []))
    for m, clases in agresivo.items():
        for c, ajustes in (clases or {}).items():
            for k in (ajustes or {}):
                if k not in seguros:
                    e.append(f"agresivo.{m}.{c}: «{k}» no está en agresivo_seguro")
    for m, clases in [*modos.items(), *(("agresivo." + m, c) for m, c in agresivo.items())]:
        if not isinstance(clases, dict) or set(clases) - set(CLASES):
            e.append(f"modos.{m}: clases solo {', '.join(CLASES)}")
            continue
        for c, ajustes in clases.items():
            for k, v in (ajustes or {}).items():
                if k not in AJUSTES:
                    e.append(f"modos.{m}.{c}: ajuste desconocido «{k}»")
                elif not _valor_ok(k, v):
                    e.append(f"modos.{m}.{c}.{k}: valor no permitido {v!r}")
    for k in p.get("agresivo_seguro", []):
        if k not in AJUSTES:
            e.append(f"agresivo_seguro: ajuste desconocido «{k}»")
    for item in p.get("lista_negra", []):
        k, _, v = str(item).partition("=")
        if k not in AJUSTES:
            e.append(f"lista_negra: ajuste desconocido «{k}»")
    return e


def esquema_json() -> dict:
    """El mismo esquema en JSON Schema, para herramientas externas (docs/perfil.schema.json)."""
    def regla(r):
        if isinstance(r, tuple):
            return {"type": "integer", "minimum": r[0], "maximum": r[1]}
        return {"enum": r}
    ajustes = {"$ref": "#/$defs/ajustes"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Perfil de equipo TISMA",
        "$defs": {"ajustes": {"type": "object", "additionalProperties": False,
                              "properties": {k: regla(r) for k, r in AJUSTES.items()}}},
        "type": "object", "required": ["formato", "version", "equipo", "limites_duros", "modos"],
        "properties": {
            "formato": {"const": FORMATO}, "version": {"type": "integer", "minimum": 1},
            "equipo": {"type": "object", "properties": {k: {"type": "string"} for k in (
                "dmi_sys_vendor", "dmi_product_name", "dmi_product_version", "cpu_modelo")}},
            "hardware": {"type": "object"},
            "limites_duros": {"type": "object", "required": list(LIMITES), "properties": {
                **{k: {"type": "integer", "minimum": lo, "maximum": hi} for k, (lo, hi) in LIMITES.items()},
                "mitigaciones": {"const": "auto"}}},
            "modos": {"type": "object", "additionalProperties": False, "properties": {
                m: {"type": "object", "additionalProperties": False,
                    "properties": {c: ajustes for c in CLASES}} for m in MODOS}},
            "agresivo": {"type": "object", "additionalProperties": False, "properties": {
                m: {"type": "object", "additionalProperties": False,
                    "properties": {c: ajustes for c in CLASES}} for m in MODOS}},
            "agresivo_seguro": {"type": "array", "items": {"enum": list(AJUSTES)}},
            "lista_negra": {"type": "array", "items": {"type": "string"}},
        }}


# ---------------------------------------------------------------- firma Ed25519 (openssl, siempre instalado)
def _canonico(p: dict) -> bytes:
    return json.dumps(p, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def firmar(perfil: dict, clave_privada_pem: str) -> bytes:
    """Firma (lado de quien publica perfiles, en SU equipo). Devuelve la firma de 64 bytes."""
    with tempfile.TemporaryDirectory() as d:
        datos = Path(d, "p.json")
        datos.write_bytes(_canonico(perfil))
        r = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", clave_privada_pem, "-rawin", "-in",
                            str(datos)], capture_output=True, check=True)
        return r.stdout


def verificar_firma(perfil: dict, firma: bytes, claves_dir: Path | None = None) -> bool:
    claves = sorted((claves_dir or CLAVES_DIR).glob("*.pem"))
    with tempfile.TemporaryDirectory() as d:
        datos, sig = Path(d, "p.json"), Path(d, "p.sig")
        datos.write_bytes(_canonico(perfil))
        sig.write_bytes(firma)
        for k in claves:
            r = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(k), "-rawin", "-in",
                                str(datos), "-sigfile", str(sig)], capture_output=True)
            if r.returncode == 0:
                return True
    return False


# ---------------------------------------------------------------- equipo y anti-retroceso
def este_equipo() -> dict:
    d = {k: sysfs.read(f"/sys/class/dmi/id/{k[4:]}") or "" for k in
         ("dmi_sys_vendor", "dmi_product_name", "dmi_product_version")}
    modelo = ""
    for linea in (sysfs.read("/proc/cpuinfo", "") or "").splitlines():
        if linea.startswith("model name"):
            modelo = linea.split(":", 1)[1].strip()
            break
    return {**d, "cpu_modelo": modelo}


def corresponde(perfil: dict, equipo: dict) -> bool:
    """Cada campo que el perfil declara tiene que coincidir (los que no declara no se comparan)."""
    return all(not v or equipo.get(k, "").strip().lower() == str(v).strip().lower()
               for k, v in (perfil.get("equipo") or {}).items())


def cargar(archivo: str, estado: Path | None = None, equipo: dict | None = None,
           claves_dir: Path | None = None) -> dict:
    """Perfil listo para usar, o PerfilInvalido con el motivo (y entonces se usa el genérico)."""
    p = json.loads(Path(archivo).read_text(encoding="utf-8"))
    errores = validar(p)
    if errores:
        raise PerfilInvalido("; ".join(errores[:5]))
    sig = Path(archivo + ".sig")
    if not sig.is_file() or not verificar_firma(p, sig.read_bytes(), claves_dir):
        raise PerfilInvalido("firma ausente o no válida")
    if not corresponde(p, equipo or este_equipo()):
        raise PerfilInvalido("el perfil es de otro modelo de equipo")
    estado = estado or Path(os.environ.get("CENTINELA_STATE", "/var/lib/centinela")) / "perfil-version.json"
    clave = json.dumps(p["equipo"], sort_keys=True)
    try:
        vistas = json.loads(estado.read_text())
    except (OSError, ValueError):
        vistas = {}
    if p["version"] < vistas.get(clave, 0):
        raise PerfilInvalido(f"versión {p['version']} más vieja que la ya aceptada ({vistas[clave]})")
    vistas[clave] = p["version"]
    estado.parent.mkdir(parents=True, exist_ok=True)
    estado.write_text(json.dumps(vistas))
    return p


def ajustes_de(perfil: dict, modo: str, clase: str, agresivo: bool = False, negra_local: set | None = None) -> dict:
    """Ajustes a aplicar: los del modo y la clase; con «agresivo», encima los extra de ese modo (solo los que el
    perfil marca como seguros en este modelo). Nunca los de la lista negra (del perfil o la local del equipo)."""
    a = dict(((perfil.get("modos") or {}).get(modo) or {}).get(clase) or {})
    if agresivo:
        seguros = set(perfil.get("agresivo_seguro", []))
        extra = ((perfil.get("agresivo") or {}).get(modo) or {}).get(clase) or {}
        a.update({k: v for k, v in extra.items() if k in seguros})
    negra = set(perfil.get("lista_negra", [])) | (negra_local or set())
    return {k: v for k, v in a.items() if k not in negra and f"{k}={v}" not in negra}
