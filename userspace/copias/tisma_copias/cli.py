"""tisma-copias: línea de órdenes (las operaciones que escriben requieren root)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import core


def _root() -> None:
    if os.geteuid() != 0:
        raise core.CopiasError("esta operación necesita permisos de administrador (pkexec tisma-copias …)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tisma-copias", description="Instantáneas del sistema y copias externas")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("estado", help="instantáneas y copia externa")
    c = sub.add_parser("instantanea", help="crea una instantánea de / y /home")
    c.add_argument("--motivo", choices=core.MOTIVOS, default="manual")
    c.add_argument("--nota", default="")
    c.add_argument("--silencioso", action="store_true", help="no falla (gancho de apt)")
    sub.add_parser("podar", help="aplica la retención")
    b = sub.add_parser("borrar")
    b.add_argument("nombre")
    r = sub.add_parser("restaurar", help="devuelve un archivo o carpeta a como estaba")
    r.add_argument("nombre")
    r.add_argument("ruta")
    v = sub.add_parser("volver-atras", help="arranca el sistema desde una instantánea (al reiniciar)")
    v.add_argument("nombre")
    v.add_argument("--si", action="store_true", help="confirmar")
    ar = sub.add_parser("autorreparacion", help="dos copias de cada dato en el disco (repara bloques dañados)")
    ar.add_argument("accion", choices=["activar", "desactivar"])
    e = sub.add_parser("externo", help="copia cifrada en un disco externo (restic)")
    esub = e.add_subparsers(dest="accion", required=True)
    ec = esub.add_parser("configurar")
    ec.add_argument("destino")
    esub.add_parser("copiar")
    esub.add_parser("comprobar")
    er = esub.add_parser("restaurar")
    er.add_argument("id")
    er.add_argument("destino")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "estado":
            st = core.estado()
            if a.json:
                print(json.dumps(st, ensure_ascii=False, indent=2))
            else:
                print(f"Raíz: {st['raiz'].get('fstype', '?')} {st['raiz'].get('subvol', '')}")
                for s in st["instantaneas"]:
                    print(f"  {s['nombre']}{'  (+home)' if s['home'] else ''}  {s['nota']}")
                ext = st["externo"]
                print("Copia externa: " + (f"{ext['destino']} ({'conectado' if ext['conectado'] else 'no conectado'})"
                                           if ext else "no configurada"))
            return 0
        _root()
        if a.cmd == "instantanea":
            try:
                name = core.crear(a.motivo, a.nota)
                print(name or "Ya hay una instantánea reciente de este tipo.")
                core.podar()
            except core.CopiasError as ex:
                if a.silencioso:
                    print(f"tisma-copias: {ex}", file=sys.stderr)
                    return 0
                raise
        elif a.cmd == "podar":
            print("\n".join(core.podar()) or "Nada que borrar.")
        elif a.cmd == "borrar":
            core.borrar(a.nombre)
        elif a.cmd == "restaurar":
            print("Restaurado: " + core.restaurar_archivo(a.nombre, a.ruta))
        elif a.cmd == "volver-atras":
            if not a.si:
                print(f"Esto hará que el sistema arranque desde {a.nombre} (tu /home no cambia). "
                      "Repite con --si para confirmar.")
                return 1
            old = core.volver_atras(a.nombre)
            print(f"Listo. Reinicia para usar {a.nombre}. El sistema actual queda guardado como {old}.")
        elif a.cmd == "autorreparacion":
            print(core.autorreparacion(a.accion == "activar"))
        elif a.cmd == "externo":
            if a.accion == "configurar":
                clave, same = core.configurar_externo(a.destino)
                print(f"Repositorio cifrado listo en {a.destino}.\nCLAVE (guárdala fuera de este equipo; sin ella "
                      f"no se puede leer la copia en otro equipo):\n\n    {clave}\n")
                if same:
                    print("AVISO: el destino está en el mismo disco que el sistema; no protege si el disco falla.")
            elif a.accion == "copiar":
                print(core.copiar_externo())
            elif a.accion == "comprobar":
                print(core.comprobar_externo())
            else:
                print("Restaurado en " + core.restaurar_externo(a.id, a.destino))
        return 0
    except core.CopiasError as ex:
        print(f"tisma-copias: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
