"""Lector del registro de Windows (formato «regf», NT 3.1 a Windows 11), solo lectura y en Python puro.

Sirve para revisar un Windows apagado desde TISMA (``escudo windows``): las colmenas SOFTWARE, SYSTEM y
NTUSER.DAT de un disco infectado son entrada hostil, así que todo se lee con comprobación de límites, sin
recursión sin tope y con detección de ciclos. Nunca escribe.

Formato (resumen): bloque base de 4096 bytes («regf») y detrás los «hbin»; las celdas empiezan con un
tamaño de 32 bits (negativo = en uso). Claves «nk», valores «vk», listas de subclaves «lf», «lh», «li» y
«ri», datos grandes «db» (desde la versión 1.4). Los desplazamientos son relativos al primer hbin.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

BASE = 4096
MAX_DEPTH = 64           # ri anidados / profundidad de claves
MAX_LIST = 1 << 20       # entradas por lista de subclaves o valores
MAX_DATA = 64 << 20      # datos de un valor

REG_NONE, REG_SZ, REG_EXPAND_SZ, REG_BINARY, REG_DWORD, REG_DWORD_BE, REG_LINK, REG_MULTI_SZ = range(8)
REG_QWORD = 11
TIPOS = {REG_NONE: "REG_NONE", REG_SZ: "REG_SZ", REG_EXPAND_SZ: "REG_EXPAND_SZ", REG_BINARY: "REG_BINARY",
         REG_DWORD: "REG_DWORD", REG_DWORD_BE: "REG_DWORD_BIG_ENDIAN", REG_LINK: "REG_LINK",
         REG_MULTI_SZ: "REG_MULTI_SZ", REG_QWORD: "REG_QWORD"}

KEY_COMP_NAME = 0x0020
VALUE_COMP_NAME = 0x0001


class RegfError(ValueError):
    """Colmena dañada o manipulada."""


@dataclass
class Value:
    name: str           # "" = valor (predeterminado)
    type: int
    raw: bytes

    @property
    def data(self):
        """Dato interpretado: str, list[str], int o bytes."""
        t, b = self.type, self.raw
        if t in (REG_SZ, REG_EXPAND_SZ, REG_LINK):
            return _utf16(b)
        if t == REG_MULTI_SZ:
            return [s for s in _utf16(b, cortar=False).split("\0") if s]
        if t == REG_DWORD and len(b) >= 4:
            return struct.unpack_from("<I", b)[0]
        if t == REG_DWORD_BE and len(b) >= 4:
            return struct.unpack_from(">I", b)[0]
        if t == REG_QWORD and len(b) >= 8:
            return struct.unpack_from("<Q", b)[0]
        return b

    def texto(self) -> str:
        d = self.data
        if isinstance(d, list):
            return " | ".join(d)
        if isinstance(d, bytes):
            # Hay programas (y malware) que guardan rutas como REG_BINARY o REG_NONE con texto UTF-16
            s = _utf16(d)
            return s if s and s.isprintable() else d.hex()
        return str(d)


def _utf16(b: bytes, cortar: bool = True) -> str:
    if len(b) % 2:
        b = b[:-1]
    s = b.decode("utf-16-le", errors="replace")
    if cortar:
        s = s.split("\0", 1)[0]
    return s.rstrip("\0")


@dataclass
class Key:
    hive: "Hive"
    offset: int
    name: str
    path: str
    flags: int
    timestamp: int                      # FILETIME (100 ns desde 1601)
    n_subkeys: int
    subkeys_off: int
    n_values: int
    values_off: int
    _hijos: dict | None = field(default=None, repr=False)

    def subkeys(self) -> list["Key"]:
        return self.hive._subkeys(self)

    def subkey(self, nombre: str) -> "Key | None":
        if self._hijos is None:
            self._hijos = {k.name.lower(): k for k in self.subkeys()}
        return self._hijos.get(nombre.lower())

    def values(self) -> list[Value]:
        return self.hive._values(self)

    def value(self, nombre: str) -> Value | None:
        n = nombre.lower()
        for v in self.values():
            if v.name.lower() == n:
                return v
        return None

    def get(self, nombre: str, defecto=None):
        v = self.value(nombre)
        return v.data if v is not None else defecto

    def walk(self, profundidad: int = MAX_DEPTH) -> Iterator["Key"]:
        yield self
        if profundidad <= 0:
            return
        for k in self.subkeys():
            yield from k.walk(profundidad - 1)

    @property
    def modificada(self) -> float:
        """Fecha de la última escritura como tiempo Unix."""
        return max(0.0, self.timestamp / 1e7 - 11644473600)


class Hive:
    """Colmena cargada entera en memoria (SOFTWARE ronda los 50–150 MB en un Windows 7 con años de uso)."""

    def __init__(self, data: bytes, nombre: str = ""):
        self.data = data
        self.nombre = nombre
        if len(data) < BASE + 32 or data[:4] != b"regf":
            raise RegfError("no es una colmena del registro (falta la firma regf)")
        seq1, seq2 = struct.unpack_from("<II", data, 4)
        self.mayor, self.menor = struct.unpack_from("<II", data, 20)
        raiz, tam = struct.unpack_from("<II", data, 36)
        # Secuencias distintas: Windows no terminó de escribir (apagado brusco o «inicio rápido»); lo último
        # puede estar solo en los .LOG. Se lee igual, pero el informe lo advierte.
        self.sucia = seq1 != seq2
        self.fin = min(len(data), BASE + tam) if tam else len(data)
        self.root = self._key(raiz, "")
        self.root.path = ""

    @classmethod
    def open(cls, ruta: str, max_bytes: int = 1 << 30) -> "Hive":
        with open(ruta, "rb") as fh:
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RegfError(f"colmena demasiado grande: {ruta}")
        return cls(data, ruta)

    # --- celdas ---
    def _cell(self, off: int) -> bytes:
        """Contenido de la celda en el desplazamiento ``off`` (relativo al primer hbin)."""
        a = BASE + off
        if off == 0xFFFFFFFF or a + 4 > self.fin:
            raise RegfError(f"celda fuera de la colmena: {off:#x}")
        size = struct.unpack_from("<i", self.data, a)[0]
        size = -size if size < 0 else size        # celdas libres: se leen igual (colmenas descuidadas)
        if size < 8 or a + size > self.fin:
            raise RegfError(f"celda con tamaño imposible en {off:#x}")
        return self.data[a + 4:a + size]

    def _key(self, off: int, padre: str) -> Key:
        c = self._cell(off)
        if len(c) < 76 or c[:2] != b"nk":
            raise RegfError(f"se esperaba una clave (nk) en {off:#x}")
        flags, ts = struct.unpack_from("<HQ", c, 2)
        n_sub, _vol, sub_off, _vsub, n_val, val_off = struct.unpack_from("<IIIIII", c, 20)
        nlen = struct.unpack_from("<H", c, 72)[0]
        raw = c[76:76 + nlen]
        if len(raw) < nlen:
            raise RegfError(f"nombre de clave truncado en {off:#x}")
        nombre = raw.decode("latin-1") if flags & KEY_COMP_NAME else _utf16(raw, cortar=False)
        return Key(self, off, nombre, f"{padre}\\{nombre}" if padre else nombre, flags, ts,
                   n_sub, sub_off, n_val, val_off)

    def _lista(self, off: int, prof: int, vistas: set[int]) -> Iterator[int]:
        if prof > MAX_DEPTH or off in vistas:
            raise RegfError("lista de subclaves circular o demasiado profunda")
        vistas.add(off)
        c = self._cell(off)
        if len(c) < 4:
            raise RegfError("lista de subclaves truncada")
        sig, n = c[:2], struct.unpack_from("<H", c, 2)[0]
        if sig in (b"lf", b"lh"):
            paso = 8
        elif sig in (b"li", b"ri"):
            paso = 4
        else:
            raise RegfError(f"lista de subclaves desconocida {sig!r} en {off:#x}")
        if 4 + n * paso > len(c):
            raise RegfError(f"lista de subclaves truncada en {off:#x}")
        for i in range(n):
            o = struct.unpack_from("<I", c, 4 + i * paso)[0]
            if sig == b"ri":
                yield from self._lista(o, prof + 1, vistas)
            else:
                yield o

    def _subkeys(self, k: Key) -> list[Key]:
        if not k.n_subkeys or k.subkeys_off == 0xFFFFFFFF:
            return []
        if k.n_subkeys > MAX_LIST:
            raise RegfError(f"demasiadas subclaves en {k.path}")
        out = []
        for o in self._lista(k.subkeys_off, 0, set()):
            if o == k.offset:
                raise RegfError(f"clave que se contiene a sí misma: {k.path}")
            out.append(self._key(o, k.path))
            if len(out) > MAX_LIST:
                raise RegfError(f"demasiadas subclaves en {k.path}")
        return out

    def _values(self, k: Key) -> list[Value]:
        if not k.n_values or k.values_off == 0xFFFFFFFF:
            return []
        if k.n_values > MAX_LIST:
            raise RegfError(f"demasiados valores en {k.path}")
        c = self._cell(k.values_off)
        if len(c) < 4 * k.n_values:
            raise RegfError(f"lista de valores truncada en {k.path}")
        out = []
        for i in range(k.n_values):
            o = struct.unpack_from("<I", c, 4 * i)[0]
            try:
                out.append(self._value(o))
            except RegfError:
                continue        # un valor dañado no impide leer los demás de la clave
        return out

    def _value(self, off: int) -> Value:
        c = self._cell(off)
        if len(c) < 20 or c[:2] != b"vk":
            raise RegfError(f"se esperaba un valor (vk) en {off:#x}")
        nlen, dlen, doff, tipo, flags = struct.unpack_from("<HIIIH", c, 2)
        raw = c[20:20 + nlen]
        nombre = raw.decode("latin-1") if flags & VALUE_COMP_NAME else _utf16(raw, cortar=False)
        if dlen & 0x80000000:                       # dato dentro del propio vk (hasta 4 bytes)
            n = dlen & 0x7FFFFFFF
            dato = struct.pack("<I", doff)[:min(n, 4)]
        elif dlen == 0:
            dato = b""
        else:
            if dlen > MAX_DATA:
                raise RegfError("dato demasiado grande")
            celda = self._cell(doff)
            if dlen > 16344 and (self.mayor, self.menor) >= (1, 4) and celda[:2] == b"db":
                dato = self._big(celda, dlen)
            else:
                dato = celda[:dlen]
        return Value(nombre, tipo, dato)

    def _big(self, celda: bytes, dlen: int) -> bytes:
        if len(celda) < 8:
            raise RegfError("dato grande (db) truncado")
        n, lista = struct.unpack_from("<HI", celda, 2)
        seg = self._cell(lista)
        if len(seg) < 4 * n:
            raise RegfError("lista de segmentos truncada")
        partes, total = [], 0
        for i in range(n):
            p = self._cell(struct.unpack_from("<I", seg, 4 * i)[0])[:16344]
            partes.append(p)
            total += len(p)
            if total >= dlen:
                break
        return b"".join(partes)[:dlen]

    # --- acceso por ruta ---
    def key(self, ruta: str) -> Key | None:
        """Clave por ruta («Microsoft\\Windows\\CurrentVersion\\Run»), sin distinguir mayúsculas."""
        k = self.root
        for parte in [p for p in ruta.replace("/", "\\").split("\\") if p]:
            k = k.subkey(parte)
            if k is None:
                return None
        return k


class Editor:
    """Cambios mínimos y en su sitio para la limpieza (nunca reubica celdas ni toca el bloque base).

    Solo tres operaciones, las que hacen falta para desactivar una persistencia: quitar un valor de una clave,
    sobrescribir una cadena si cabe en su celda y sobrescribir un DWORD. Si la colmena está «sucia» (Windows
    tiene cambios pendientes en los .LOG que reaplicaría al arrancar), no se toca.
    """

    def __init__(self, ruta: str):
        self.ruta = ruta
        with open(ruta, "rb") as fh:
            self.buf = bytearray(fh.read())
        self.hive = Hive(bytes(self.buf), ruta)
        if self.hive.sucia:
            raise RegfError("la colmena tiene cambios pendientes de Windows (apagado incompleto o «inicio rápido»): "
                            "arranca Windows, apágalo con «shutdown /s /t 0» y vuelve a intentarlo")
        self.cambios = 0

    def _abs(self, off: int) -> int:
        return BASE + off + 4                       # inicio de los datos de la celda

    def _releer(self) -> None:
        self.hive = Hive(bytes(self.buf), self.ruta)

    def borrar_valor(self, clave: str, nombre: str) -> bool:
        k = self.hive.key(clave)
        if k is None:
            return False
        lista = self.hive._cell(k.values_off) if k.n_values else b""
        offs = [struct.unpack_from("<I", lista, 4 * i)[0] for i in range(k.n_values)]
        quedan = [o for o in offs if self._nombre_vk(o).lower() != nombre.lower()]
        if len(quedan) == len(offs):
            return False
        a = self._abs(k.values_off)
        for i, o in enumerate(quedan):
            struct.pack_into("<I", self.buf, a + 4 * i, o)
        for i in range(len(quedan), len(offs)):
            struct.pack_into("<I", self.buf, a + 4 * i, 0)
        struct.pack_into("<I", self.buf, self._abs(k.offset) + 36, len(quedan))   # nk: número de valores
        self.cambios += 1
        self._releer()
        return True

    def _nombre_vk(self, off: int) -> str:
        try:
            return self.hive._value(off).name
        except RegfError:
            return "\0"     # valor dañado: nunca coincide con un nombre buscado

    def _vk(self, clave: str, nombre: str) -> tuple[int, bytes]:
        k = self.hive.key(clave)
        if k is None:
            raise RegfError(f"no existe la clave {clave}")
        lista = self.hive._cell(k.values_off) if k.n_values else b""
        for i in range(k.n_values):
            o = struct.unpack_from("<I", lista, 4 * i)[0]
            if self._nombre_vk(o).lower() == nombre.lower():
                return o, self.hive._cell(o)
        raise RegfError(f"no existe el valor {nombre} en {clave}")

    def poner_cadena(self, clave: str, nombre: str, texto: str) -> bool:
        o, c = self._vk(clave, nombre)
        nlen, dlen, doff, tipo = struct.unpack_from("<HIII", c, 2)
        if tipo not in (REG_SZ, REG_EXPAND_SZ):
            raise RegfError(f"{nombre} no es una cadena")
        nuevo = (texto + "\0").encode("utf-16-le")
        if dlen & 0x80000000 or dlen > 16344:
            raise RegfError(f"{nombre}: dato en un formato que no se edita en su sitio")
        celda = self.hive._cell(doff)
        if len(nuevo) > len(celda):
            raise RegfError(f"{nombre}: el valor nuevo no cabe en su sitio")
        a = self._abs(doff)
        self.buf[a:a + len(celda)] = nuevo + b"\0" * (len(celda) - len(nuevo))
        struct.pack_into("<I", self.buf, self._abs(o) + 4, len(nuevo))
        self.cambios += 1
        self._releer()
        return True

    def poner_dword(self, clave: str, nombre: str, valor: int) -> bool:
        o, c = self._vk(clave, nombre)
        _nlen, dlen, _doff, tipo = struct.unpack_from("<HIII", c, 2)
        if tipo != REG_DWORD or dlen != 0x80000004:
            raise RegfError(f"{nombre} no es un DWORD")
        struct.pack_into("<I", self.buf, self._abs(o) + 8, valor & 0xFFFFFFFF)
        self.cambios += 1
        self._releer()
        return True

    def guardar(self) -> None:
        """Escritura atómica (archivo temporal en la misma carpeta + rename)."""
        import os
        if not self.cambios:
            return
        tmp = self.ruta + ".tisma-tmp"
        with open(tmp, "wb") as fh:
            fh.write(self.buf)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.ruta)
