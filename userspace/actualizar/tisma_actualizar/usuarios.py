"""Unir las cuentas del sistema instalado con las de la versión nueva y renumerar los dueños de los archivos.

El sistema nuevo trae sus cuentas de sistema (sddm, polkitd…) con números que pueden no coincidir con los del
instalado. Las personas y sus números se conservan tal cual (sus archivos de /home no se tocan); las cuentas de
sistema que solo existen en la versión nueva se añaden, con otro número si el suyo ya estaba ocupado. Después,
los archivos del sistema nuevo pasan a los números finales (mapa nuevo → final).
"""
from __future__ import annotations

import errno
import os
import stat


def leer(texto: str) -> list[list[str]]:
    return [linea.split(":") for linea in texto.splitlines() if linea.strip() and not linea.startswith("#")]


def escribir(filas: list[list[str]]) -> str:
    return "".join(":".join(f) + "\n" for f in filas)


def _libre(usados: set[int], desde: int = 100, hasta: int = 999) -> int:
    for n in range(hasta, desde - 1, -1):        # de arriba abajo, como adduser --system
        if n not in usados:
            return n
    raise RuntimeError("no quedan números libres para cuentas del sistema")


def unir(viejo: list[list[str]], nuevo: list[list[str]], grupos: bool = False) -> tuple[list[list[str]], dict[int, int]]:
    """passwd (columna 2 = uid) o group (columna 2 = gid, columna 3 = miembros). Devuelve (filas finales, mapa número_nuevo → número_final)."""
    col = 2
    final = [list(f) for f in viejo]
    por_nombre = {f[0]: f for f in final}
    usados = {int(f[col]) for f in final}
    mapa: dict[int, int] = {}
    for f in nuevo:
        n_nuevo = int(f[col])
        if f[0] in por_nombre:
            mapa[n_nuevo] = int(por_nombre[f[0]][col])
            if grupos and len(f) > 3 and len(por_nombre[f[0]]) > 3:      # group: miembros sumados
                miembros = [m for m in por_nombre[f[0]][3].split(",") if m]
                for m in f[3].split(","):
                    if m and m not in miembros:
                        miembros.append(m)
                por_nombre[f[0]][3] = ",".join(miembros)
            continue
        fila = list(f)
        if n_nuevo in usados:
            fila[col] = str(_libre(usados))
        usados.add(int(fila[col]))
        mapa[n_nuevo] = int(fila[col])
        final.append(fila)
        por_nombre[f[0]] = fila
    return final, mapa


def gid_principal(passwd: list[list[str]], mapa_gid: dict[int, int], anadidas: set[str]) -> list[list[str]]:
    """Las cuentas AÑADIDAS guardan su grupo principal con el número de la versión nueva: se traduce. Las que ya
    estaban conservan el suyo (es un número del sistema instalado, no de la versión nueva)."""
    return [f[:3] + [str(mapa_gid.get(int(f[3]), int(f[3])))] + f[4:] if len(f) > 3 and f[0] in anadidas else f
            for f in passwd]


def unir_sombra(viejo: list[list[str]], nuevo: list[list[str]], nombres: set[str]) -> list[list[str]]:
    """shadow/gshadow: las contraseñas del instalado; solo se añaden las cuentas nuevas."""
    tiene = {f[0] for f in viejo}
    return [list(f) for f in viejo] + [list(f) for f in nuevo if f[0] not in tiene and f[0] in nombres]


def solo_cambios(mapa: dict[int, int]) -> dict[int, int]:
    return {a: b for a, b in mapa.items() if a != b}


def renumerar(raiz: str, uids: dict[int, int], gids: dict[int, int]) -> int:
    """Cambia los dueños en todo el árbol (sin salir del sistema de archivos). chown borra los bits setuid/setgid y
    las capacidades de archivo (ping, etc.): se vuelven a poner. Devuelve cuántos cambió."""
    if not uids and not gids:
        return 0
    dev = os.lstat(raiz).st_dev
    cambiados = 0
    for base, dirs, archivos in os.walk(raiz):
        dirs[:] = [d for d in dirs if os.lstat(os.path.join(base, d)).st_dev == dev]
        for nombre in [*dirs, *archivos]:
            p = os.path.join(base, nombre)
            st = os.lstat(p)
            u, g = uids.get(st.st_uid, st.st_uid), gids.get(st.st_gid, st.st_gid)
            if (u, g) == (st.st_uid, st.st_gid):
                continue
            cap = None
            if not stat.S_ISLNK(st.st_mode):
                try:
                    cap = os.getxattr(p, "security.capability", follow_symlinks=False)
                except OSError as e:
                    if e.errno not in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
                        raise
            os.lchown(p, u, g)
            if not stat.S_ISLNK(st.st_mode):
                if st.st_mode & (stat.S_ISUID | stat.S_ISGID):
                    os.chmod(p, stat.S_IMODE(st.st_mode))
                if cap is not None:
                    os.setxattr(p, "security.capability", cap, follow_symlinks=False)
            cambiados += 1
    return cambiados
