"""Consola del equipo de seguridad de TISMA para las emergencias de la IA escolar.

  generar-claves <archivo-privada>         crea el par; la privada va a <archivo> (600, FUERA del servidor) y se
                                           muestra la pública para /etc/tisma-ia-escolar/emergencias.pub
  pendientes
  ver <id> <archivo-privada>               queda registrado en la auditoría antes de mostrar nada
  resolver <id> falso_positivo|escuela|autoridades [nota]   borra el contenido sellado
  auditoria <id>

Quien revisa se identifica con TISMA_REVISOR (p. ej. su nombre y apellido).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import emergencias as em
from .datos import Datos


def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if a[:1] == ["generar-claves"] and len(a) == 2:
        priv, pub = em.generar_claves()
        fd = os.open(a[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(priv)
        sys.stdout.buffer.write(pub)
        return 0
    if not a:
        print(__doc__)
        return 2
    datos = Datos(os.environ.get("TISMA_IA_DB", "/var/lib/tisma-ia-escolar/datos.db"),
                  Path(os.environ.get("TISMA_IA_PIMIENTA_ARCHIVO", "/etc/tisma-ia-escolar/pimienta")).read_bytes())
    c = em.Consola(datos, os.environ.get("TISMA_REVISOR", ""))
    cmd = a[0]
    if cmd == "pendientes":
        for e in c.pendientes():
            print(json.dumps(e, ensure_ascii=False))
    elif cmd == "ver" and len(a) == 3:
        print(json.dumps(c.ver(int(a[1]), Path(a[2]).read_bytes()), ensure_ascii=False, indent=1))
    elif cmd == "resolver" and len(a) >= 3:
        c.resolver(int(a[1]), a[2], " ".join(a[3:]))
        print("resuelta; el contenido sellado se borró")
    elif cmd == "auditoria" and len(a) == 2:
        for fila in c.auditoria(int(a[1])):
            print(*fila, sep="\t")
    else:
        print(__doc__)
        return 2
    return 0
