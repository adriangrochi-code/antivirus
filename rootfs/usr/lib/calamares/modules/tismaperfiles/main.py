#!/usr/bin/env python3
"""Guarda los usos elegidos en la página «Tu uso» (packagechooserq@perfiles) en /etc/tisma/perfiles-elegidos del
sistema instalado. Luego «tisma-perfil completar» (que ya corre al final de la instalación y, sin conexión, en
cada arranque hasta lograrlo) descarga la base y TODAS las apps de lo elegido, sumadas."""
import os

import libcalamares

PERFILES = ("diario", "estudio", "ingenieria", "programacion", "juegos")


def pretty_name():
    return "Guardando los usos elegidos"


def elegidos(valor) -> list:
    """«juegos,programacion» o «todo» → lista de perfiles válidos, sin repetir y en orden."""
    ids = [x.strip() for x in str(valor or "").split(",") if x.strip()]
    if "todo" in ids:
        return list(PERFILES)
    return [x for x in dict.fromkeys(ids) if x in PERFILES]


def run():
    gs = libcalamares.globalstorage
    perfiles = elegidos(gs.value("packagechooser_perfiles"))
    raiz = gs.value("rootMountPoint")
    if not raiz:
        return ("No se encontró el sistema instalado", "rootMountPoint vacío")
    ruta = os.path.join(raiz, "etc/tisma/perfiles-elegidos")
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write("".join(p + "\n" for p in perfiles))
    os.chmod(ruta, 0o644)              # el instalador corre con umask 002: que solo root pueda cambiarlo
    libcalamares.utils.debug("Usos elegidos: " + (", ".join(perfiles) or "ninguno"))
    return None
