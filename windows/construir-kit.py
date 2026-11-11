#!/usr/bin/env python3
"""Prepara el kit de endurecimiento para Windows: saltos de línea CRLF, .bat en ASCII puro (cmd lee con la página
de códigos OEM, distinta según el idioma) y LEEME.txt en UTF-8 con BOM (el Bloc de notas de XP lo entiende).

  construir-kit.py <destino>
"""
import os
import sys

ORIGEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kit")


CI = r"HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Microsoft\Windows\Safer\CodeIdentifiers"
SHELL = r"%HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
# Reglas «No permitido» de las directivas de restricción de software. Van en un .reg (hex(2): bytes exactos) y no
# en el .bat, porque cmd expandiría los %VARIABLE% al instalar y las reglas quedarían fijas en las carpetas del
# administrador en vez de las de cada usuario.
REGLAS = [
    (r"%TEMP%\*.*", "Temp"),
    (r"%TEMP%\*\*.*", "Temp, un nivel"),
    (r"%TEMP%\*\*\*.*", "Temp, dos niveles"),
    (r"%APPDATA%\*.*", "AppData"),
    (r"%APPDATA%\*\*.*", "AppData, un nivel"),
    (SHELL + r"\Local AppData%\*.*", "AppData local"),
    (SHELL + r"\Local AppData%\*\*.*", "AppData local, un nivel"),
    (r"%USERPROFILE%\Downloads", "Descargas"),
    (SHELL + r"\Desktop%\*.scr", "Escritorio: salvapantallas"),
    (SHELL + r"\Desktop%\*.pif", "Escritorio: pif"),
    (r"?:\RECYCLER", "Papelera (XP)"),
    (r"?:\$Recycle.Bin", "Papelera"),
    (r"%ProgramData%\*.*", "ProgramData (raiz)"),
    (r"%PUBLIC%", "Acceso publico"),
    (r"?:\System Volume Information", "Restauracion del sistema"),
]
# Tipos vigilados: la lista de Windows SIN «LNK» (si no, se rompen los accesos directos del menú Inicio)
TIPOS = ("ADE ADP BAS BAT CHM CMD COM CPL CRT EXE HLP HTA INF INS ISP JS JSE MDB MDE MSC MSI MSP MST OCX PCD PIF "
         "PS1 REG SCR SHS URL VB VBE VBS WSC WSF WSH").split()


def _hex(tipo: int, datos: bytes) -> str:
    return f"hex({tipo:x}):" + ",".join(f"{b:02x}" for b in datos)


def _sz(s: str) -> bytes:
    return (s + "\0").encode("utf-16-le")


def reg_srp() -> str:
    """Archivo .reg (formato 5.00, UTF-16) con la política y las reglas."""
    l = ["Windows Registry Editor Version 5.00", "", f"[{CI}]",
         '"TISMA"=dword:00000001', '"DefaultLevel"=dword:00040000', '"TransparentEnabled"=dword:00000001',
         '"PolicyScope"=dword:00000000', '"AuthenticodeEnabled"=dword:00000000',
         '"ExecutableTypes"=' + _hex(7, "".join(t + "\0" for t in TIPOS).encode("utf-16-le") + b"\0\0"), ""]
    for i, (ruta, desc) in enumerate(REGLAS, 1):
        l += [f"[{CI}\\0\\Paths\\{{7154A5E0-7153-4D41-8000-{i:012d}}}]",
              '"ItemData"=' + _hex(2, _sz(ruta)), '"SaferFlags"=dword:00000000',
              f'"Description"="TISMA: {desc}"', ""]
    return "\r\n".join(l) + "\r\n"


def construir(destino: str) -> list[str]:
    os.makedirs(destino, exist_ok=True)
    hechos = []
    for nombre in sorted(os.listdir(ORIGEN)):
        texto = open(os.path.join(ORIGEN, nombre), encoding="utf-8").read().replace("\r\n", "\n")
        if nombre.lower().endswith((".bat", ".cmd")) or nombre == "plantilla-deshacer.txt":
            malas = [i + 1 for i, l in enumerate(texto.split("\n")) if not l.isascii()]
            if malas:
                raise SystemExit(f"{nombre}: caracteres no ASCII en las líneas {malas}")
            datos = texto.replace("\n", "\r\n").encode("ascii")
        else:
            datos = "﻿".encode() + texto.replace("\n", "\r\n").encode("utf-8")
        with open(os.path.join(destino, nombre), "wb") as fh:
            fh.write(datos)
        hechos.append(nombre)
    with open(os.path.join(destino, "tisma-srp.reg"), "wb") as fh:
        fh.write("\ufeff".encode("utf-16-le") + reg_srp().encode("utf-16-le"))
    hechos.append("tisma-srp.reg")
    return hechos


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print("\n".join(construir(sys.argv[1])))
