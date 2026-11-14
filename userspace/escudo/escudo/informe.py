"""Informe claro de lo que hizo un programa al analizarlo (idea de ANY.RUN, pero en el equipo y en simple).

El análisis (detonar.py) deja motivos técnicos («intentó leer un secreto (señuelo): ~/.config/chromium/…»).
Aquí se traducen a frases para cualquiera, agrupadas por tema, y el detalle técnico queda aparte.

Los informes se guardan POR USUARIO (/var/lib/escudo-informes/<uid>/, solo esa persona los lee): en un equipo
compartido de una escuela, los nombres de archivo de cada uno son privados. Nada sale del equipo.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

DIR = Path(os.environ.get("ESCUDO_INFORMES_DIR", "/var/lib/escudo-informes"))
MAX_POR_USUARIO = 50

# (patrón sobre el motivo técnico, grupo, frase simple). El primero que coincide gana.
REGLAS = [
    (r"secreto.*(chromium|chrome|edge|firefox|mozilla|Login Data|Cookies|logins\.json|Local State)", "datos",
     "Intentó robar las contraseñas o sesiones guardadas del navegador."),
    (r"secreto.*\.ssh", "datos", "Intentó robar tus claves de acceso a otros equipos."),
    (r"secreto.*(electrum|exodus|wallet)", "datos", "Intentó robar carteras de criptomonedas."),
    (r"secreto.*telegram", "datos", "Intentó robar tu sesión de Telegram."),
    (r"secreto.*keyring", "datos", "Intentó leer tu llavero de contraseñas."),
    (r"secreto.*\.aws", "datos", "Intentó robar claves de servicios en la nube."),
    (r"secreto", "datos", "Intentó leer datos privados guardados en el equipo."),
    (r"ransomware", "documentos",
     "Modificó o borró muchos documentos: se comporta como un secuestrador de archivos (ransomware)."),
    (r"modificó \d+ documento", "documentos", "Modificó documentos."),
    (r"quedarse en el arranque|arrancar solo|carpeta Inicio", "arranque",
     "Intentó quedarse instalado para arrancar solo cada vez que enciendes el equipo."),
    (r"programar tareas", "arranque", "Intentó programar tareas para volver a ejecutarse más tarde."),
    (r"minería", "internet", "Se conectó a un servicio de minería de criptomonedas (usaría tu equipo para ganar "
                             "dinero para otros)."),
    (r"escaneo o red de bots", "internet",
     "Intentó conectarse con muchísimas direcciones, como hacen los equipos infectados."),
    (r"descargó o creó un programa", "esconderse",
     "Descargó o creó otro programa y lo ejecutó (así suelen llegar los virus más peligrosos)."),
    (r"desde memoria", "esconderse", "Intentó ejecutarse sin dejar archivo, para esconderse de los antivirus."),
    (r"borró su propio archivo", "esconderse", "Borró su propio archivo para no dejar rastro."),
    (r"inyectarse", "esconderse", "Intentó meterse dentro de otros programas abiertos."),
    (r"privilegios de administrador|convertirse en administrador", "permisos",
     "Intentó conseguir permisos de administrador."),
    (r"montar sistemas de archivos|señales a todos", "permisos", "Intentó tocar partes del sistema que no le tocan."),
    (r"^detectado: (.+)", "conocido", "Lo reconoció la base de virus conocidos ({0})."),
]
GRUPOS = {"conocido": "Virus conocido", "datos": "Tus datos y contraseñas", "documentos": "Tus documentos",
          "arranque": "Arranque automático", "esconderse": "Esconderse", "permisos": "Permisos",
          "internet": "Internet", "otros": "Otras cosas"}
FRASE = {"limpio": "No hizo nada peligroso mientras lo probamos aislado.",
         "sospechoso": "Hizo cosas raras. No lo abras si no confías en quién te lo dio.",
         "malicioso": "Es peligroso. Escudo lo bloqueó y no pudo hacer daño."}


def _frase(motivo: str) -> tuple[str, str]:
    for patron, grupo, frase in REGLAS:
        m = re.search(patron, motivo, re.I)
        if m:
            return grupo, frase.format(*m.groups())
    return "otros", motivo[:1].upper() + motivo[1:]


def explicar(inf: dict, nombre: str = "") -> dict:
    """{veredicto, titulo, frase, grupos: [{titulo, puntos}], tecnico: [...]} a partir de Informe.to_dict()."""
    veredicto = inf.get("veredicto", "limpio")
    grupos: dict[str, list[str]] = {}
    for motivo in inf.get("motivos", []):
        g, f = _frase(motivo)
        if f not in grupos.setdefault(g, []):
            grupos[g].append(f)
    conexiones = inf.get("conexiones") or []
    if conexiones and veredicto != "limpio":
        grupos.setdefault("internet", []).append(
            f"Intentó conectarse a internet ({len(conexiones)} dirección{'es' if len(conexiones) != 1 else ''}).")
    otros = sorted({os.path.basename(e) for e in inf.get("ejecuciones") or [] if e})
    if otros and veredicto != "limpio":
        grupos.setdefault("otros", []).append("Abrió otros programas: " + ", ".join(otros[:6]) +
                                              ("…" if len(otros) > 6 else "") + ".")
    tecnico = list(inf.get("motivos", [])) + [f"conexión: {c}" for c in conexiones[:30]] + \
        [f"ejecutó: {e}" for e in (inf.get("ejecuciones") or [])[:30]]
    if inf.get("entorno"):
        tecnico.append(f"analizado en: {inf['entorno']}" + (f" ({inf['segundos']:.0f} s)" if inf.get("segundos")
                                                             else ""))
    if inf.get("sha256"):
        tecnico.append(f"SHA-256: {inf['sha256']}")
    titulo = {"limpio": "Sin peligro", "sospechoso": "Sospechoso", "malicioso": "Peligroso"}.get(veredicto, veredicto)
    return {"veredicto": veredicto, "titulo": f"{nombre}: {titulo}" if nombre else titulo,
            "frase": FRASE.get(veredicto, ""),
            "grupos": [{"titulo": GRUPOS[g], "puntos": grupos[g]} for g in GRUPOS if grupos.get(g)],
            "tecnico": tecnico}


def guardar(inf: dict, ruta: str, uid: int, base: Path | None = None, ahora=time.time) -> Path | None:
    """Guarda el informe en la carpeta privada de ese usuario (como root). Conserva los últimos 50."""
    if uid <= 0 or not re.fullmatch(r"[0-9a-f]{64}", inf.get("sha256", "")):
        return None
    base = base or DIR
    base.mkdir(mode=0o711, parents=True, exist_ok=True)
    d = base / str(uid)
    d.mkdir(mode=0o700, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(d, uid, -1)
    datos = {"nombre": os.path.basename(ruta)[:200], "fecha": ahora(), "informe": inf}
    f = d / f"{inf['sha256']}.json"
    fd = os.open(f.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(datos, fh, ensure_ascii=False)
    os.replace(f.with_suffix(".tmp"), f)
    if os.geteuid() == 0:
        os.chown(f, uid, -1)
    viejos = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-MAX_POR_USUARIO]
    for v in viejos:
        v.unlink(missing_ok=True)
    return f


def leer(uid: int, base: Path | None = None, limite: int = 20) -> list[dict]:
    """Los informes de ESE usuario, del más nuevo al más viejo, ya explicados."""
    d = (base or DIR) / str(uid)
    salida = []
    try:
        archivos = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limite]
    except OSError:
        return []
    for f in archivos:
        try:
            datos = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        e = explicar(datos.get("informe", {}), datos.get("nombre", ""))
        e.update(nombre=datos.get("nombre", ""), fecha=datos.get("fecha", 0))
        salida.append(e)
    return sorted(salida, key=lambda e: e["fecha"], reverse=True)       # por fecha del análisis
