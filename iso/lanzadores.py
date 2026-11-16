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


# Palabras de búsqueda: quien viene de otro sistema busca el programa que ya conoce y encuentra el equivalente
# (en el menú y en la Tienda, antes de instalarlo). No se muestran en pantalla: solo se usan al buscar.
# lanzador → (ids de AppStream para la Tienda, palabras)
PALABRAS = {
    "libreoffice-writer.desktop": (["org.libreoffice.LibreOffice.writer"],
                                   "word;docx;doc;procesador de textos;documento de texto;"),
    "libreoffice-calc.desktop": (["org.libreoffice.LibreOffice.calc"], "excel;xlsx;xls;hoja de calculo;planilla;"),
    "libreoffice-impress.desktop": (["org.libreoffice.LibreOffice.impress"],
                                    "powerpoint;pptx;ppt;presentacion;diapositivas;"),
    "libreoffice-draw.desktop": (["org.libreoffice.LibreOffice.draw"], "visio;publisher;diagrama;afiche;"),
    "org.gimp.GIMP.desktop": (["org.gimp.GIMP"], "photoshop;editar fotos;retocar fotos;"),
    "gimp.desktop": (["org.gimp.GIMP"], "photoshop;editar fotos;retocar fotos;"),
    "org.inkscape.Inkscape.desktop": (["org.inkscape.Inkscape"], "illustrator;corel;coreldraw;vectores;logo;"),
    "org.kde.krita.desktop": (["org.kde.krita"], "sai;clip studio;dibujo digital;ilustracion;"),
    "org.kde.kolourpaint.desktop": (["org.kde.kolourpaint"], "paint;dibujar;"),
    "org.kde.kdenlive.desktop": (["org.kde.kdenlive"], "premiere;vegas;filmora;movie maker;editar video;"),
    "com.obsproject.Studio.desktop": (["com.obsproject.Studio"], "grabar pantalla;streaming;transmitir;"),
    "org.mozilla.Thunderbird.desktop": (["org.mozilla.Thunderbird"], "outlook;correo;email;mail;"),
    "thunderbird.desktop": (["org.mozilla.Thunderbird"], "outlook;correo;email;mail;"),
    "org.kde.okular.desktop": (["org.kde.okular"], "acrobat;adobe reader;lector pdf;"),
    "org.kde.kate.desktop": (["org.kde.kate"], "bloc de notas;notepad;notepad++;"),
    "org.kde.spectacle.desktop": (["org.kde.spectacle"], "recortes;snipping;captura;"),
    "org.kde.dolphin.desktop": (["org.kde.dolphin"], "explorador;explorer;mis documentos;mi pc;"),
    "org.kde.ark.desktop": (["org.kde.ark"], "winrar;7zip;zip;rar;comprimir;descomprimir;"),
    "org.kde.haruna.desktop": (["org.kde.haruna"], "vlc;reproductor;media player;peliculas;"),
    "org.kde.elisa.desktop": (["org.kde.elisa"], "itunes;reproductor de musica;"),
    "org.kde.gwenview.desktop": (["org.kde.gwenview"], "visor de fotos;imagenes;"),
    "org.kde.plasma-systemmonitor.desktop": (["org.kde.plasma-systemmonitor"],
                                             "administrador de tareas;task manager;procesos;"),
    "org.freecad.FreeCAD.desktop": (["org.freecad.FreeCAD"], "solidworks;autocad;inventor;fusion;cad;"),
    "org.kicad.kicad.desktop": (["org.kicad.kicad"], "altium;eagle;proteus;circuito impreso;pcb;"),
    "blender.desktop": (["org.blender.Blender"], "3ds max;maya;cinema 4d;modelado 3d;"),
    "audacity.desktop": (["org.audacityteam.Audacity"], "editar audio;grabar voz;"),
    "net.tisma.Navegador.desktop": ([], "chrome;edge;internet;"),
    "net.tisma.Preferencias.desktop": ([], "panel de control;configuracion;"),
    "tisma-tienda.desktop": ([], "store;play store;app store;"),
}


def palabras(texto: str, extra: str) -> str:
    """Suma palabras a Keywords= (sin repetir) solo en [Desktop Entry]."""
    cab, sep, resto = texto.partition("\n[Desktop Action")
    m = re.search(r"(?m)^Keywords=(.*)$", cab)
    actuales = [k for k in (m.group(1) if m else "").split(";") if k]
    nuevas = actuales + [k for k in extra.split(";") if k and k not in actuales]
    linea = "Keywords=" + ";".join(nuevas) + ";"
    cab = re.sub(r"(?m)^Keywords=.*$", lambda _: linea, cab, count=1) if m else cab.rstrip("\n") + "\n" + linea + "\n"
    return cab + sep + resto


def catalogo_appstream() -> str:
    """Catálogo AppStream que suma estas palabras a los programas de la Tienda (merge=append)."""
    from xml.sax.saxutils import escape
    comps = {}
    for ids, extra in PALABRAS.values():
        for i in ids:
            comps.setdefault(i, [])
            comps[i] += [k for k in extra.split(";") if k and k not in comps[i]]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<components version="0.16" origin="tisma-equivalentes">']
    for i, ks in sorted(comps.items()):
        out.append(f'  <component merge="append"><id>{escape(i)}</id><keywords>' +
                   "".join(f"<keyword>{escape(k)}</keyword>" for k in ks) + "</keywords></component>")
    return "\n".join(out + ["</components>", ""])


# Lanzadores cuya orden cambia: (orden original, orden TISMA)
EXEC = {"opensnitch_ui.desktop": ("opensnitch-ui", "tisma-opensnitch")}


def cambiar_exec(texto: str, viejo: str, nuevo: str) -> str:
    cab, sep, resto = texto.partition("\n[Desktop Action")
    cab = re.sub(rf"(?m)^(Exec|TryExec)=(\S*/)?{re.escape(viejo)}\b", rf"\1={nuevo}", cab)
    return cab + sep + resto


def aplicar(raiz: str) -> list[str]:
    origen = Path(raiz, "usr/share/applications")
    destino = Path(raiz, "usr/local/share/applications")
    hechos = []
    for nombre in sorted(set(AVANZADO) | set(RENOMBRAR) | set(PALABRAS) | set(EXEC)):
        f = origen / nombre
        if not f.is_file():
            # El programa se desinstaló: sin su copia (quedaría un lanzador roto en el menú)
            (destino / nombre).unlink(missing_ok=True)
            continue
        texto = f.read_text(errors="replace")
        if nombre in RENOMBRAR:
            texto = renombrar(texto, RENOMBRAR[nombre])
        if nombre in AVANZADO:
            texto = avanzado(texto)
        if nombre in EXEC:
            texto = cambiar_exec(texto, *EXEC[nombre])
        if nombre in PALABRAS:
            texto = palabras(texto, PALABRAS[nombre][1])
        destino.mkdir(parents=True, exist_ok=True)
        (destino / nombre).write_text(texto)
        hechos.append(nombre)
    return hechos


if __name__ == "__main__":
    hechos = aplicar(sys.argv[1])
    cat = Path(sys.argv[1], "usr/share/swcatalog/xml/tisma-equivalentes.xml")
    cat.parent.mkdir(parents=True, exist_ok=True)
    if not cat.is_file() or cat.read_text() != catalogo_appstream():
        cat.write_text(catalogo_appstream())
    print("\n".join(hechos))
