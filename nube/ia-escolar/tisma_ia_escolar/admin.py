"""Alta de escuelas, grupos, docentes y códigos de vinculación (lo usa el equipo de TISMA o la institución).

  tisma-ia-escolar-admin institucion "Escuela N.º 123"
  tisma-ia-escolar-admin grupo <institucion> "4.º A" primaria
  tisma-ia-escolar-admin docente <institucion> <grupo> [<grupo>…]   → muestra su ficha UNA vez
  tisma-ia-escolar-admin codigo <grupo> [usos] [horas]               → código de 8 letras para la clase
  tisma-ia-escolar-admin baja <cuenta>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .datos import Datos


def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if not a:
        print(__doc__)
        return 2
    db = os.environ.get("TISMA_IA_DB", "/var/lib/tisma-ia-escolar/datos.db")
    pimienta = Path(os.environ.get("TISMA_IA_PIMIENTA_ARCHIVO", "/etc/tisma-ia-escolar/pimienta")).read_bytes()
    d = Datos(db, pimienta)
    cmd, args = a[0], a[1:]
    if cmd == "institucion" and len(args) == 1:
        print(d.crear_institucion(args[0]))
    elif cmd == "grupo" and len(args) == 3 and args[2] in ("primaria", "liceo"):
        print(d.crear_grupo(*args))
    elif cmd == "docente" and len(args) >= 2:
        cuenta, ficha = d.crear_docente(args[0], args[1:])
        print(f"cuenta: {cuenta}\nficha (guárdala: no se vuelve a mostrar): {ficha}")
    elif cmd == "codigo" and 1 <= len(args) <= 3:
        print(d.crear_codigo(args[0], int(args[1]) if len(args) > 1 else 40, float(args[2]) if len(args) > 2 else 72))
    elif cmd == "baja" and len(args) == 1:
        d.dar_de_baja(args[0])
        print("cuenta dada de baja")
    else:
        print(__doc__)
        return 2
    return 0
