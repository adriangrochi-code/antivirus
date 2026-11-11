"""Escritor mínimo de colmenas regf para las pruebas (no se instala: solo genera registros de Windows falsos).

arbol = {"valores": {"Nombre": (tipo, bytes) | str | int | list[str]}, "claves": {"Sub": arbol, …}}
"""
from __future__ import annotations

import struct

REG_SZ, REG_EXPAND_SZ, REG_BINARY, REG_DWORD, REG_MULTI_SZ = 1, 2, 3, 4, 7


def _sz(s: str) -> bytes:
    return (s + "\0").encode("utf-16-le")


def _valor(v):
    if isinstance(v, tuple):
        return v
    if isinstance(v, bool) or isinstance(v, int):
        return REG_DWORD, struct.pack("<I", v & 0xFFFFFFFF)
    if isinstance(v, list):
        return REG_MULTI_SZ, "".join(x + "\0" for x in v).encode("utf-16-le") + b"\0\0"
    return REG_SZ, _sz(v)


class _Escritor:
    def __init__(self, ri_cada: int = 0, version=(1, 5)):
        self.datos = bytearray(32)       # cabecera del hbin: los desplazamientos de celda cuentan desde aquí
        self.ri_cada = ri_cada
        self.version = version

    def celda(self, contenido: bytes) -> int:
        tam = (len(contenido) + 4 + 7) & ~7
        off = len(self.datos)
        self.datos += struct.pack("<i", -tam) + contenido + b"\0" * (tam - 4 - len(contenido))
        return off

    def vk(self, nombre: str, tipo: int, dato: bytes) -> int:
        n = nombre.encode("latin-1")
        if len(dato) <= 4:
            dlen, doff = 0x80000000 | len(dato), struct.unpack("<I", dato.ljust(4, b"\0"))[0]
        elif len(dato) > 16344:
            segs = [self.celda(dato[i:i + 16344]) for i in range(0, len(dato), 16344)]
            lista = self.celda(b"".join(struct.pack("<I", s) for s in segs))
            dlen, doff = len(dato), self.celda(b"db" + struct.pack("<HI", len(segs), lista))
        else:
            dlen, doff = len(dato), self.celda(dato)
        return self.celda(b"vk" + struct.pack("<HIIIHH", len(n), dlen, doff, tipo, 1, 0) + n)

    def nk(self, nombre: str, arbol: dict, padre: int, raiz: bool = False) -> int:
        # El nk se escribe primero (con huecos) para conocer su desplazamiento y dárselo a los hijos
        n = nombre.encode("latin-1")
        off = self.celda(b"nk" + b"\0" * 74 + n)
        vals = [self.vk(k, *_valor(v)) for k, v in arbol.get("valores", {}).items()]
        vlist = self.celda(b"".join(struct.pack("<I", o) for o in vals)) if vals else 0xFFFFFFFF
        hijos = [(k, self.nk(k, sub, off)) for k, sub in sorted(arbol.get("claves", {}).items(),
                                                                 key=lambda x: x[0].upper())]
        if not hijos:
            slist = 0xFFFFFFFF
        elif self.ri_cada and len(hijos) > self.ri_cada:
            partes = [hijos[i:i + self.ri_cada] for i in range(0, len(hijos), self.ri_cada)]
            subs = [self.celda(b"li" + struct.pack("<H", len(p)) + b"".join(struct.pack("<I", o) for _, o in p))
                    for p in partes]
            slist = self.celda(b"ri" + struct.pack("<H", len(subs)) + b"".join(struct.pack("<I", o) for o in subs))
        else:
            slist = self.celda(b"lh" + struct.pack("<H", len(hijos)) +
                               b"".join(struct.pack("<II", o, 0) for _, o in hijos))
        flags = 0x20 | (0x4 | 0x8 if raiz else 0)
        cab = b"nk" + struct.pack("<HQIIIIIIIIIIIIIIIHH", flags, 0, 0, padre, len(hijos), 0, slist, 0xFFFFFFFF,
                                  len(vals), vlist, 0xFFFFFFFF, 0xFFFFFFFF, 0, 0, 0, 0, 0, len(n), 0)
        a = off + 4
        self.datos[a:a + len(cab)] = cab
        return off

    def colmena(self, arbol: dict, sucia: bool = False) -> bytes:
        raiz = self.nk("ROOT", arbol, 0xFFFFFFFF, raiz=True)
        tam = (len(self.datos) + 8 + 4095) & ~4095
        self.datos[:32] = b"hbin" + struct.pack("<IIQQI", 0, tam, 0, 0, 0)
        libre = tam - len(self.datos)
        self.datos += struct.pack("<i", libre) + b"\0" * (libre - 4)
        return self._base(raiz, tam, sucia) + bytes(self.datos)

    def _base(self, raiz: int, tam: int, sucia: bool) -> bytes:
        b = bytearray(4096)
        b[:4] = b"regf"
        struct.pack_into("<IIQIIII", b, 4, 1, 2 if sucia else 1, 0, self.version[0], self.version[1], 0, 1)
        struct.pack_into("<II", b, 36, raiz, tam)
        x = 0
        for i in range(0, 508, 4):
            x ^= struct.unpack_from("<I", b, i)[0]
        struct.pack_into("<I", b, 508, x)
        return bytes(b)


def colmena(arbol: dict, ri_cada: int = 0, sucia: bool = False) -> bytes:
    return _Escritor(ri_cada).colmena(arbol, sucia)


def arbol(**claves) -> dict:
    """Atajo: arbol(**{"A\\B\\C": {"Valor": "x"}}) → árbol anidado."""
    raiz: dict = {}
    for ruta, valores in claves.items():
        n = raiz
        for p in ruta.split("\\"):
            n = n.setdefault("claves", {}).setdefault(p, {})
        n.setdefault("valores", {}).update(valores)
    return raiz
