#!/usr/bin/env python3
"""Menú limpio: lo técnico o repetido va a la categoría «Avanzado» (no se borra nada) y se corrigen nombres.

Copia el .desktop a /usr/local/share/applications (tiene prioridad y las actualizaciones del paquete no lo
pisan) con la categoría X-TISMA-Avanzado o el nombre corregido. Todo sigue apareciendo al buscar.
Uso: lanzadores.py <raíz>
"""
import re
import sys
from pathlib import Path

# A «Avanzado» (al final del menú, sigue apareciendo al buscar): lo que una persona normal no usa a diario.
AVANZADO = [
    "chromium.desktop",                      # el navegador es TISMA Navegador (aislado); este va sin aislar
    "kdesystemsettings.desktop",             # Preferencias completas de KDE (repetidas)
    "org.kde.discover.desktop",              # la Tienda TISMA lo abre con su nombre
    "bleachbit-root.desktop", "lynis.desktop", "opensnitch_ui.desktop", "org.rnd2.cpupower_gui.desktop",
    "vim.desktop", "org.kde.konsole.desktop", "org.kde.kwatchgnupg.desktop", "org.kde.kwalletmanager.desktop",
    "org.kde.kleopatra.desktop", "org.kde.drkonqi.coredump.gui.desktop", "torbrowser-settings.desktop",
    "org.kde.kdeconnect.nonplasma.desktop", "org.kde.partitionmanager.desktop", "org.kde.kcharselect.desktop",
    "org.kde.kfind.desktop", "org.kde.kinfocenter.desktop", "org.kde.plasma-systemmonitor.desktop",
    "fr.romainvigier.MetadataCleaner.desktop", "org.kde.filelight.desktop", "org.qbittorrent.qBittorrent.desktop",
    "torbrowser.desktop", "Waydroid.desktop", "waydroid.market.desktop", "waydroid.app.install.desktop",
    "org.keepassxc.KeePassXC.desktop", "system-config-printer.desktop", "tisma-revisar-windows.desktop",
    "calamares.desktop",                     # el instalador, en un sistema ya instalado
]
RENOMBRAR = {
    "com.github.jeromerobert.pdfarranger.desktop": "Organizar PDF",   # en español salía «Ventana nueva»
    "org.kde.partitionmanager.desktop": "Discos y particiones",
    # Nombres que dicen qué hace cada programa (el nombre propio queda como descripción debajo)
    "org.kde.kcalc.desktop": "Calculadora científica",
    "org.kde.kmag.desktop": "Lupa",
    "org.kde.kmousetool.desktop": "Clic automático",
    "org.kde.kate.desktop": "Editor de texto",
    "org.kde.ark.desktop": "Archivos comprimidos",
    "org.kde.spectacle.desktop": "Capturas de pantalla",
    "org.kde.gwenview.desktop": "Fotos",
    "org.kde.haruna.desktop": "Vídeos",
    "org.kde.elisa.desktop": "Música",
    "org.kde.okular.desktop": "Visor de documentos",
    "org.kde.kolourpaint.desktop": "Dibujo",
    "org.kde.kamoso.desktop": "Cámara",
    "org.kde.skanpage.desktop": "Escáner",
    "org.kde.kweather.desktop": "El tiempo",
}


def avanzado(texto: str) -> str:
    """Añade la categoría X-TISMA-Avanzado (el menú la saca del resto de categorías y la pone en «Avanzado»)."""
    cab, sep, resto = texto.partition("\n[Desktop Action")
    if re.search(r"(?m)^Categories=.*X-TISMA-Avanzado", cab):
        return texto
    if re.search(r"(?m)^Categories=", cab):
        cab = re.sub(r"(?m)^Categories=(.*?);?$", r"Categories=\1;X-TISMA-Avanzado;", cab, count=1)
    else:
        cab = cab.rstrip("\n") + "\nCategories=X-TISMA-Avanzado;\n"
    return cab + sep + resto


def renombrar(texto: str, nombre: str) -> str:
    """Cambia Name y Name[xx] solo en [Desktop Entry] (no en las acciones)."""
    cab, sep, resto = texto.partition("\n[Desktop Action")
    cab = re.sub(r"(?m)^Name(\[[^]]*\])?=.*$", lambda m: f"Name{m.group(1) or ''}={nombre}", cab)
    return cab + sep + resto


def aplicar(raiz: str) -> list[str]:
    origen = Path(raiz, "usr/share/applications")
    destino = Path(raiz, "usr/local/share/applications")
    hechos = []
    for nombre in sorted(set(AVANZADO) | set(RENOMBRAR)):
        f = origen / nombre
        if not f.is_file():
            continue
        texto = f.read_text(errors="replace")
        if nombre in RENOMBRAR:
            texto = renombrar(texto, RENOMBRAR[nombre])
        if nombre in AVANZADO:
            texto = avanzado(texto)
        destino.mkdir(parents=True, exist_ok=True)
        (destino / nombre).write_text(texto)
        hechos.append(nombre)
    return hechos


if __name__ == "__main__":
    print("\n".join(aplicar(sys.argv[1])))
