#!/usr/bin/env python3
"""Menú limpio: oculta lanzadores técnicos o repetidos y corrige nombres confusos.

Copia el .desktop a /usr/local/share/applications (tiene prioridad y las actualizaciones del paquete no lo
pisan) con NoDisplay=true o con el nombre corregido. NoDisplay lo saca también de la búsqueda: por eso solo se
ocultan cosas técnicas, repetidas o que tienen su versión TISMA. Uso: lanzadores.py <raíz>
"""
import re
import sys
from pathlib import Path

OCULTAR = {
    "chromium.desktop": "el navegador es TISMA Navegador (aislado)",
    "kdesystemsettings.desktop": "repetido: ya está Preferencias del sistema",
    "org.kde.discover.desktop": "la Tienda TISMA lo abre con su nombre",
    "bleachbit-root.desktop": "versión como administrador (peligrosa)",
    "lynis.desktop": "auditoría por terminal (está en TISMA Seguridad)",
    "opensnitch_ui.desktop": "interfaz técnica del cortafuegos de apps",
    "org.rnd2.cpupower_gui.desktop": "frecuencias de la CPU (Centinela lo ajusta solo)",
    "vim.desktop": "editor de terminal",
    "org.kde.kwatchgnupg.desktop": "registro técnico de GnuPG",
    "org.kde.kwalletmanager.desktop": "gestor técnico de carteras",
    "org.kde.drkonqi.coredump.gui.desktop": "visor de fallos para programadores",
    "torbrowser-settings.desktop": "ajustes del lanzador de Tor",
    "org.kde.kdeconnect.nonplasma.desktop": "indicador para otros escritorios",
}
RENOMBRAR = {
    "com.github.jeromerobert.pdfarranger.desktop": "Organizar PDF",   # en español salía «Ventana nueva»
    "org.kde.partitionmanager.desktop": "Discos y particiones",
}


def ocultar(texto: str) -> str:
    sin = re.sub(r"(?m)^(NoDisplay|Hidden)=.*\n?", "", texto)
    return re.sub(r"(?m)^\[Desktop Entry\]\n", "[Desktop Entry]\nNoDisplay=true\n", sin, count=1)


def renombrar(texto: str, nombre: str) -> str:
    """Cambia Name y Name[xx] solo en [Desktop Entry] (no en las acciones)."""
    cab, sep, resto = texto.partition("\n[Desktop Action")
    cab = re.sub(r"(?m)^Name(\[[^]]*\])?=.*$", lambda m: f"Name{m.group(1) or ''}={nombre}", cab)
    return cab + sep + resto


def aplicar(raiz: str) -> list[str]:
    origen = Path(raiz, "usr/share/applications")
    destino = Path(raiz, "usr/local/share/applications")
    hechos = []
    for nombre in sorted(set(OCULTAR) | set(RENOMBRAR)):
        f = origen / nombre
        if not f.is_file():
            continue
        texto = f.read_text(errors="replace")
        texto = ocultar(texto) if nombre in OCULTAR else renombrar(texto, RENOMBRAR[nombre])
        destino.mkdir(parents=True, exist_ok=True)
        (destino / nombre).write_text(texto)
        hechos.append(nombre)
    return hechos


if __name__ == "__main__":
    print("\n".join(aplicar(sys.argv[1])))
