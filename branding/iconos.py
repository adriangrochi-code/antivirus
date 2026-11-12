#!/usr/bin/env python3
"""Iconos de las aplicaciones de TISMA: una familia, no un logo repetido.

Lo común (marca): el hexágono del logo, en grafito, con el mismo borde claro y un brillo violeta arriba.
Lo propio de cada app: un símbolo sencillo y grueso (se reconoce a 16 px) con el color de esa app. Los colores son
de la misma familia (saturación y luminosidad parecidas) y están separados en el círculo cromático para que dos
apps no se confundan en la barra de tareas.

  iconos.py <carpeta de destino>      → escribe tisma-<app>.svg
"""
from __future__ import annotations

import sys
from pathlib import Path

# Hexágono del logo (vértice arriba), escalado a 256x256 con margen
HEX = "M128 10 L230 69 L230 187 L128 246 L26 187 L26 69 Z"

# app: (color claro, color oscuro, símbolo SVG en un lienzo de 256 centrado en 128,128)
TRAZO = 'fill="none" stroke="url(#g)" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"'
FINO = TRAZO.replace('stroke-width="18"', 'stroke-width="12"')
APPS: dict[str, tuple[str, str, str]] = {
    # Bienvenida: destello (lo nuevo, el primer arranque)
    "bienvenida": ("#C084FC", "#8B3FE0",
                   '<path d="M128 64 C134 104 152 122 192 128 C152 134 134 152 128 192 C122 152 104 134 64 128 '
                   'C104 122 122 104 128 64 Z" fill="url(#g)"/>'),
    # Asistente: bocadillo de conversación con tres puntos
    "asistente": ("#2DD4BF", "#0F9A8A",
                  f'<path d="M70 86 H186 Q198 86 198 98 V154 Q198 166 186 166 H120 L92 190 V166 H70 Q58 166 58 154 '
                  f'V98 Q58 86 70 86 Z" {TRAZO}/>'
                  '<g fill="url(#g)"><circle cx="98" cy="126" r="10"/><circle cx="128" cy="126" r="10"/>'
                  '<circle cx="158" cy="126" r="10"/></g>'),
    # Code: </>
    "code": ("#60A5FA", "#2563EB",
             f'<path d="M96 84 L56 128 L96 172 M160 84 L200 128 L160 172 M142 72 L114 184" {TRAZO}/>'),
    # Navegador: globo
    "navegador": ("#38BDF8", "#0284C7",
                  f'<circle cx="128" cy="128" r="66" {TRAZO}/>'
                  f'<path d="M62 128 H194 M128 62 C100 92 100 164 128 194 C156 164 156 92 128 62" {TRAZO}/>'),
    # Rendimiento: indicador con aguja
    "rendimiento": ("#FBBF24", "#D97706",
                    f'<path d="M62 166 A70 70 0 1 1 194 166" {TRAZO}/>'
                    f'<path d="M128 150 L166 102" {TRAZO}/><circle cx="128" cy="150" r="13" fill="url(#g)"/>'),
    # Instalar TISMA OS: flecha hacia el disco
    "instalar": ("#E879F9", "#C026D3",
                 f'<path d="M128 60 V140 M94 108 L128 142 L162 108 M66 162 V186 H190 V162" {TRAZO}/>'),
    # Drivers y firmware: chip
    "drivers": ("#4ADE80", "#16A34A",
                f'<rect x="80" y="80" width="96" height="96" rx="12" {TRAZO}/>'
                f'<path d="M104 80 V58 M128 80 V58 M152 80 V58 M104 176 V198 M128 176 V198 M152 176 V198 '
                f'M80 104 H58 M80 128 H58 M80 152 H58 M176 104 H198 M176 128 H198 M176 152 H198" {FINO}/>'
                '<rect x="110" y="110" width="36" height="36" rx="5" fill="url(#g)"/>'),
    # Revisar Windows: escudo con ventana de cuatro paneles
    "revisar-windows": ("#FB7185", "#E11D48",
                        f'<path d="M128 56 L188 78 V126 C188 162 162 186 128 200 C94 186 68 162 68 126 V78 Z" '
                        f'{TRAZO}/>'
                        '<g fill="url(#g)"><rect x="100" y="98" width="24" height="24" rx="3"/>'
                        '<rect x="132" y="98" width="24" height="24" rx="3"/>'
                        '<rect x="100" y="130" width="24" height="24" rx="3"/>'
                        '<rect x="132" y="130" width="24" height="24" rx="3"/></g>'),
    # SolidCraft (CAD): cubo isométrico
    "solidcraft": ("#818CF8", "#4F46E5",
                   f'<path d="M128 60 L188 94 V162 L128 196 L68 162 V94 Z M68 94 L128 128 L188 94 M128 128 V196" '
                   f'{TRAZO}/>'),
    # Seguridad (revisar virus, rootkits, USB, red…): escudo con una tilde
    "seguridad": ("#FB923C", "#EA580C",
                  f'<path d="M128 56 L188 78 V126 C188 162 162 186 128 200 C94 186 68 162 68 126 V78 Z" {TRAZO}/>'
                  f'<path d="M100 128 L120 148 L158 108" {TRAZO}/>'),
    # Guía: libro abierto
    "guia": ("#A3E635", "#65A30D",
             f'<path d="M128 84 C108 70 82 66 58 70 V178 C82 174 108 178 128 192 C148 178 174 174 198 178 V70 '
             f'C174 66 148 70 128 84 Z M128 84 V192" {TRAZO}/>'),
    # Tienda: bolsa
    "tienda": ("#F472B6", "#DB2777",
               f'<path d="M72 100 H184 L174 192 H82 Z" {TRAZO}/>'
               f'<path d="M104 112 V90 C104 58 152 58 152 90 V112" {TRAZO}/>'),
}


def icono(nombre: str) -> str:
    claro, oscuro, simbolo = APPS[nombre]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">
  <defs>
    <linearGradient id="fondo" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#2E2C35"/><stop offset="1" stop-color="#18171C"/>
    </linearGradient>
    <radialGradient id="brillo" cx="0.5" cy="0.12" r="0.75">
      <stop offset="0" stop-color="#A55BE8" stop-opacity="0.35"/><stop offset="1" stop-color="#A55BE8" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{claro}"/><stop offset="1" stop-color="{oscuro}"/>
    </linearGradient>
  </defs>
  <path d="{HEX}" fill="url(#fondo)"/>
  <path d="{HEX}" fill="url(#brillo)"/>
  <path d="{HEX}" fill="none" stroke="#E6E6EA" stroke-width="8" stroke-linejoin="round"/>
  {simbolo}
</svg>
'''


def escribir(destino: Path) -> list[Path]:
    destino.mkdir(parents=True, exist_ok=True)
    hechos = []
    for nombre in APPS:
        p = destino / f"tisma-{nombre}.svg"
        p.write_text(icono(nombre), encoding="utf-8")
        hechos.append(p)
    return hechos


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for p in escribir(Path(sys.argv[1])):
        print(p)
