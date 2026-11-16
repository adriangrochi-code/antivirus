"""ECC por software, nivel «Verificar» (documento de traspaso §6.1). OPCIONAL y desactivado por defecto.

Qué hace: detecta y corrige ALGUNOS errores de memoria en los modelos de IA locales (los datos más grandes y más
leídos del equipo). NO sustituye a la memoria con ECC: no protege el sistema, ni los programas, ni lo que se
corrompe entre dos revisiones; y una huella (hash) no protege frente a un atacante (para eso: firmas y aislamiento).

Cómo:
* Manifiesto: huella BLAKE2b de cada bloque de 4 MiB del archivo, tomada del disco (btrfs ya verifica sus
  propias sumas al leer).
* Revisión en ráfagas (prioridad mínima de CPU y disco, nunca mientras la IA genera): se lee la copia que está
  en la memoria (caché de páginas, la que usa el motor) y se compara bloque a bloque.
* Si un bloque no coincide: se descarta esa parte de la caché (posix_fadvise DONTNEED) y se vuelve a leer desde
  el disco verificado. Si coincide → corregido. Si el motor tiene ese trozo en uso (mapeado y fijado), no se puede
  descartar: se deja un aviso para que TISMA Code reinicie el motor. Si tampoco coincide releyendo del disco,
  el archivo cambió o el disco falla: se avisa.
* Si aparecen errores, se RECOMIENDA revisar la memoria (Memory test); no se cambia nada solo.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import random
import time
from pathlib import Path

from .apply import STATE_DIR, load_state, save_state

BLOQUE = 4 << 20
DIR = Path(STATE_DIR) / "ecc"
ALERTA = Path(os.environ.get("CENTINELA_RUN", "/run/centinela")) / "ecc-alerta.json"
NIVELES = ("desactivado", "verificar")
TEXTO = ("Detecta y corrige algunos errores en los modelos de IA y en datos críticos; no sustituye a la memoria "
         "con ECC.")


def nivel() -> str:
    n = (load_state("prefs.json") or {}).get("ecc", "desactivado")
    return n if n in NIVELES else "desactivado"


def poner_nivel(n: str) -> str:
    if n not in NIVELES:
        raise ValueError(f"nivel: {', '.join(NIVELES)}")
    prefs = load_state("prefs.json") or {}
    prefs["ecc"] = n
    save_state("prefs.json", prefs)
    return n


def _h(datos: bytes) -> str:
    return hashlib.blake2b(datos, digest_size=16).hexdigest()


def _ruta_manifiesto(archivo: str) -> Path:
    return DIR / (hashlib.sha256(os.path.realpath(archivo).encode()).hexdigest()[:32] + ".json")


def manifiesto(archivo: str, forzar: bool = False) -> dict:
    """Huellas por bloque (se rehacen si el archivo cambió de tamaño o de fecha)."""
    st = os.stat(archivo)
    m = _ruta_manifiesto(archivo)
    try:
        d = json.loads(m.read_text())
        if not forzar and d["tam"] == st.st_size and d["mtime"] == st.st_mtime_ns:
            return d
    except (OSError, ValueError, KeyError):
        pass
    huellas = []
    with open(archivo, "rb") as fh:
        while datos := fh.read(BLOQUE):
            huellas.append(_h(datos))
    d = {"archivo": os.path.realpath(archivo), "tam": st.st_size, "mtime": st.st_mtime_ns, "huellas": huellas}
    DIR.mkdir(parents=True, exist_ok=True)
    m.write_text(json.dumps(d))
    return d


def revisar(archivo: str, leer=None, descartar=None, pausar=lambda: False, max_bloques: int | None = None,
            desde: int = 0) -> dict:
    """Compara la copia en memoria con el manifiesto. Devuelve {revisados, errores, corregidos, sin_corregir,
    siguiente} (para seguir en la próxima ráfaga)."""
    d = manifiesto(archivo)
    res = {"revisados": 0, "errores": 0, "corregidos": 0, "sin_corregir": [], "siguiente": desde}
    fd = os.open(archivo, os.O_RDONLY)
    leer = leer or (lambda i: os.pread(fd, BLOQUE, i * BLOQUE))
    descartar = descartar or (lambda i: os.posix_fadvise(fd, i * BLOQUE, BLOQUE, os.POSIX_FADV_DONTNEED))
    try:
        i = desde
        while i < len(d["huellas"]):
            if pausar():
                break
            if max_bloques is not None and res["revisados"] >= max_bloques:
                break
            if _h(leer(i)) != d["huellas"][i]:
                res["errores"] += 1
                descartar(i)                                   # fuera la copia de la RAM
                if _h(leer(i)) == d["huellas"][i]:             # releída del disco (btrfs la verifica)
                    res["corregidos"] += 1
                else:
                    res["sin_corregir"].append(i)
            res["revisados"] += 1
            i += 1
        res["siguiente"] = i if i < len(d["huellas"]) else 0
    finally:
        os.close(fd)
    return res


def modelos() -> list[str]:
    return sorted(glob.glob("/home/*/.local/share/tisma-code/models/*/*.gguf"))


def rafaga(mb: int = 512, pausar=None) -> dict:
    """Una ráfaga del revisor: hasta `mb` MiB entre todos los modelos, continuando donde quedó la anterior.
    Pausa si la IA está generando (no le quita ancho de banda)."""
    if nivel() != "verificar":
        return {"estado": "desactivado"}
    if pausar is None:
        from .modo_ia import activo
        pausar = activo
    estado = load_state("ecc-estado.json") or {}
    restante = max(1, (mb << 20) // BLOQUE)
    total = {"revisados": 0, "errores": 0, "corregidos": 0, "sin_corregir": 0}
    for m in modelos():
        if restante <= 0 or pausar():
            break
        r = revisar(m, pausar=pausar, max_bloques=restante, desde=estado.get(m, 0))
        estado[m] = r["siguiente"]
        restante -= r["revisados"]
        for k in ("revisados", "errores", "corregidos"):
            total[k] += r[k]
        total["sin_corregir"] += len(r["sin_corregir"])
        if r["sin_corregir"]:
            _alertar(m, r["sin_corregir"])
    acum = estado.setdefault("_total", {"errores": 0, "corregidos": 0})
    acum["errores"] += total["errores"]
    acum["corregidos"] += total["corregidos"]
    estado["_ultima"] = time.time()
    save_state("ecc-estado.json", estado)
    return {"estado": "ok", **total, "acumulado": acum}


def _alertar(archivo: str, bloques: list[int]) -> None:
    """Para TISMA Code: el motor tiene ese modelo en uso con datos dañados; hay que volver a cargarlo."""
    try:
        ALERTA.parent.mkdir(parents=True, exist_ok=True)
        ALERTA.write_text(json.dumps({"t": time.time(), "archivo": archivo, "bloques": bloques[:20]}))
        os.chmod(ALERTA, 0o644)
    except OSError:
        pass


def prueba_inyeccion(tam_mb: int = 64, fallos: int = 200, semilla: int = 1) -> dict:
    """Voltear bits a propósito en una copia y medir cuántos se detectan y se corrigen (desde la copia buena) y a
    qué velocidad se revisa (costo)."""
    rnd = random.Random(semilla)
    bueno = bytes(rnd.getrandbits(8) for _ in range(1 << 16)) * ((tam_mb << 20) // (1 << 16))
    huellas = [_h(bueno[i:i + BLOQUE]) for i in range(0, len(bueno), BLOQUE)]
    malo = bytearray(bueno)
    tocados = set()
    for _ in range(fallos):
        pos = rnd.randrange(len(malo))
        malo[pos] ^= 1 << rnd.randrange(8)
        tocados.add(pos // BLOQUE)
    t0 = time.perf_counter()
    detectados = {i for i, h in enumerate(huellas) if _h(bytes(malo[i * BLOQUE:(i + 1) * BLOQUE])) != h}
    seg = time.perf_counter() - t0
    for i in detectados:                                   # «recarga desde disco»: la copia buena
        malo[i * BLOQUE:(i + 1) * BLOQUE] = bueno[i * BLOQUE:(i + 1) * BLOQUE]
    corregidos = sum(1 for i in detectados if _h(bytes(malo[i * BLOQUE:(i + 1) * BLOQUE])) == huellas[i])
    return {"bloques_con_fallos": len(tocados), "detectados": len(detectados & tocados),
            "corregidos": corregidos, "falsos": len(detectados - tocados),
            "pct_detectado": round(100 * len(detectados & tocados) / max(1, len(tocados)), 1),
            "velocidad_gbs": round(tam_mb / 1024 / seg, 2) if seg else None}
