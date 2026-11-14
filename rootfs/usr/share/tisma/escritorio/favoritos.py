#!/usr/bin/python3 -I
"""Favoritos del menú de cuentas que ya existían (p. ej. tras actualizar): cambia los lanzadores viejos por los de
TISMA. Kickoff guarda los favoritos en la base de kactivitymanagerd, no en su configuración: el diseño inicial solo
llega a cuentas nuevas. Solo cambia esas filas concretas; si algo no cuadra, no toca nada."""
import os
import sqlite3
import sys

CAMBIOS = {
    "applications:org.kde.discover.desktop": "applications:tisma-tienda.desktop",
    "applications:chromium.desktop": "applications:net.tisma.Navegador.desktop",
    "preferred://browser": "applications:net.tisma.Navegador.desktop",
    # Las preferencias simples en el menú; la Configuración del sistema queda en «Opciones avanzadas»
    "applications:systemsettings.desktop": "applications:net.tisma.Preferencias.desktop",
}
AGREGAR = ["applications:net.tisma.Guia.desktop", "applications:net.tisma.Seguridad.desktop"]
AGENTE = "org.kde.plasma.favorites.applications"


def cambiar(db: str) -> int:
    if not os.path.isfile(db):
        return 0
    con = sqlite3.connect(db, timeout=10)
    try:
        filas = 0
        with con:
            for viejo, nuevo in CAMBIOS.items():
                # Si ya está el nuevo para esa actividad, se borra el viejo en vez de duplicarlo
                con.execute("DELETE FROM ResourceLink WHERE targettedResource = ? AND usedActivity IN "
                            "(SELECT usedActivity FROM ResourceLink WHERE targettedResource = ?)", (viejo, nuevo))
                filas += con.execute("UPDATE ResourceLink SET targettedResource = ? WHERE targettedResource = ? "
                                     "AND initiatingAgent LIKE 'org.kde.plasma.favorites%'", (nuevo, viejo)).rowcount
            # La Guía y TISMA Seguridad, si la cuenta ya tenía favoritos (no se crean favoritos donde no había)
            actividades = [r[0] for r in con.execute(
                "SELECT DISTINCT usedActivity FROM ResourceLink WHERE initiatingAgent = ?", (AGENTE,))]
            for act in actividades:
                for app in AGREGAR:
                    filas += con.execute("INSERT OR IGNORE INTO ResourceLink VALUES (?, ?, ?)",
                                         (act, AGENTE, app)).rowcount
        return filas
    finally:
        con.close()


if __name__ == "__main__":
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    try:
        print(cambiar(sys.argv[1] if len(sys.argv) > 1 else os.path.join(base, "kactivitymanagerd/resources/database")))
    except sqlite3.Error as e:
        print(f"sin cambios: {e}")
