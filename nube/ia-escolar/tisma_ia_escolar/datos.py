"""Cuentas escolares, cupos y lo único que se guarda del uso (SQLite).

Lo que NO se guarda nunca: el texto de las preguntas ni de las respuestas. Por cada mensaje queda solo: cuándo, de
qué cuenta (pseudónima), el tema (matemática, lengua…) y los tokens; y, si hubo riesgo, una alerta con la categoría
y un resumen genérico. Los nombres reales los tiene la escuela, no TISMA: aquí cada estudiante es un identificador.

Fichas (tokens): solo se guarda su HMAC con una clave secreta del servidor («pimienta»): una copia robada de la
base de datos no sirve para entrar.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS instituciones (id TEXT PRIMARY KEY, nombre TEXT NOT NULL, creada REAL NOT NULL);
CREATE TABLE IF NOT EXISTS grupos (id TEXT PRIMARY KEY, institucion TEXT NOT NULL REFERENCES instituciones(id),
    nombre TEXT NOT NULL, nivel TEXT NOT NULL CHECK (nivel IN ('primaria', 'liceo')));
CREATE TABLE IF NOT EXISTS cuentas (id TEXT PRIMARY KEY, tipo TEXT NOT NULL CHECK (tipo IN ('estudiante', 'docente')),
    institucion TEXT NOT NULL REFERENCES instituciones(id), grupo TEXT REFERENCES grupos(id),
    ficha TEXT UNIQUE NOT NULL, activa INTEGER NOT NULL DEFAULT 1, creada REAL NOT NULL, alias TEXT);
CREATE TABLE IF NOT EXISTS docente_grupos (docente TEXT NOT NULL REFERENCES cuentas(id),
    grupo TEXT NOT NULL REFERENCES grupos(id), PRIMARY KEY (docente, grupo));
CREATE TABLE IF NOT EXISTS codigos (codigo TEXT PRIMARY KEY, grupo TEXT NOT NULL REFERENCES grupos(id),
    expira REAL NOT NULL, usos INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS uso (dia TEXT NOT NULL, cuenta TEXT NOT NULL, mensajes INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (dia, cuenta));
CREATE TABLE IF NOT EXISTS temas (dia TEXT NOT NULL, grupo TEXT NOT NULL, tema TEXT NOT NULL,
    mensajes INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (dia, grupo, tema));
CREATE TABLE IF NOT EXISTS alertas (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, cuenta TEXT NOT NULL,
    grupo TEXT NOT NULL, categoria TEXT NOT NULL, resumen TEXT NOT NULL, atendida INTEGER NOT NULL DEFAULT 0);
"""
CUPO_DIARIO = {"primaria": 40, "liceo": 120}       # mensajes por estudiante y día
RETENCION_DIAS = {"uso": 90, "temas": 365, "alertas": 365}
RESUMEN = {"autolesion": "Posible riesgo de autolesión: hablar hoy con el estudiante y seguir el protocolo de la "
                         "institución.",
           "abuso": "Posible situación de abuso o maltrato: seguir el protocolo de la institución (no confrontar a "
                    "la familia sin asesoramiento).",
           "acoso": "Posible acoso escolar (bullying): conversar con el estudiante y el equipo de la institución."}


def _id(prefijo: str) -> str:
    return f"{prefijo}_{secrets.token_hex(8)}"


class Datos:
    def __init__(self, ruta: str | Path, pimienta: bytes):
        if len(pimienta) < 32:
            raise ValueError("la clave del servidor (pimienta) debe tener al menos 32 bytes")
        self.pimienta = pimienta
        self.db = sqlite3.connect(str(ruta), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(ESQUEMA)
        self.lock = threading.Lock()

    def _q(self, sql: str, args=()) -> list[tuple]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def huella(self, ficha: str) -> str:
        return hmac.new(self.pimienta, ficha.encode(), hashlib.sha256).hexdigest()

    # ------------------------------------------------------------ alta (la hace la escuela)
    def crear_institucion(self, nombre: str) -> str:
        i = _id("inst")
        self._q("INSERT INTO instituciones VALUES (?, ?, ?)", (i, nombre[:120], time.time()))
        return i

    def crear_grupo(self, institucion: str, nombre: str, nivel: str) -> str:
        g = _id("grp")
        self._q("INSERT INTO grupos VALUES (?, ?, ?, ?)", (g, institucion, nombre[:60], nivel))
        return g

    def crear_docente(self, institucion: str, grupos: list[str]) -> tuple[str, str]:
        ficha = "doc_" + secrets.token_urlsafe(32)
        d = _id("doc")
        self._q("INSERT INTO cuentas VALUES (?, 'docente', ?, NULL, ?, 1, ?, NULL)",
                (d, institucion, self.huella(ficha), time.time()))
        for g in grupos:
            self._q("INSERT INTO docente_grupos VALUES (?, ?)", (d, g))
        return d, ficha

    def crear_codigo(self, grupo: str, usos: int = 40, horas: float = 72) -> str:
        """Código para vincular los equipos de un grupo (lo reparte el docente en clase): corto y de un rato."""
        alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"            # sin 0/O ni 1/I: se dicta fácil
        codigo = "".join(secrets.choice(alfabeto) for _ in range(8))
        self._q("INSERT INTO codigos VALUES (?, ?, ?, ?)", (self.huella(codigo), grupo, time.time() + horas * 3600, usos))
        return codigo

    def vincular(self, codigo: str, alias: str = "") -> tuple[str, str] | None:
        """Canjea un código por una cuenta de estudiante nueva y su ficha (None si no vale). El alias (p. ej. «Sofía,
        n.º 12») es lo único que permite al docente saber quién es ante una alerta: lo mínimo, y solo lo ve él."""
        alias = " ".join(alias.split())[:40]
        h = self.huella(codigo.strip().upper())
        with self.lock:
            fila = self.db.execute("SELECT grupo, expira, usos FROM codigos WHERE codigo = ?", (h,)).fetchone()
            if not fila or fila[1] < time.time() or fila[2] <= 0:
                return None
            inst = self.db.execute("SELECT institucion FROM grupos WHERE id = ?", (fila[0],)).fetchone()[0]
            ficha = "est_" + secrets.token_urlsafe(32)
            e = _id("est")
            self.db.execute("UPDATE codigos SET usos = usos - 1 WHERE codigo = ?", (h,))
            self.db.execute("INSERT INTO cuentas VALUES (?, 'estudiante', ?, ?, ?, 1, ?, ?)",
                            (e, inst, fila[0], self.huella(ficha), time.time(), alias or None))
        return e, ficha

    def dar_de_baja(self, cuenta: str) -> None:
        self._q("UPDATE cuentas SET activa = 0 WHERE id = ?", (cuenta,))

    # ------------------------------------------------------------ uso
    def cuenta(self, ficha: str) -> dict | None:
        fila = self._q("SELECT c.id, c.tipo, c.institucion, c.grupo, g.nivel FROM cuentas c LEFT JOIN grupos g "
                       "ON g.id = c.grupo WHERE c.ficha = ? AND c.activa = 1", (self.huella(ficha),))
        if not fila:
            return None
        i, tipo, inst, grupo, nivel = fila[0]
        return {"id": i, "tipo": tipo, "institucion": inst, "grupo": grupo, "nivel": nivel}

    def cupo_restante(self, cuenta: dict) -> int:
        dia = time.strftime("%Y-%m-%d")
        fila = self._q("SELECT mensajes FROM uso WHERE dia = ? AND cuenta = ?", (dia, cuenta["id"]))
        return CUPO_DIARIO.get(cuenta["nivel"] or "", 0) - (fila[0][0] if fila else 0)

    def registrar(self, cuenta: dict, tema: str, tokens: int, riesgo: str | None) -> None:
        dia = time.strftime("%Y-%m-%d")
        self._q("INSERT INTO uso VALUES (?, ?, 1, ?) ON CONFLICT(dia, cuenta) DO UPDATE SET mensajes = mensajes + 1, "
                "tokens = tokens + excluded.tokens", (dia, cuenta["id"], int(tokens)))
        self._q("INSERT INTO temas VALUES (?, ?, ?, 1) ON CONFLICT(dia, grupo, tema) DO UPDATE SET "
                "mensajes = mensajes + 1", (dia, cuenta["grupo"], tema))
        if riesgo:
            self._q("INSERT INTO alertas (ts, cuenta, grupo, categoria, resumen) VALUES (?, ?, ?, ?, ?)",
                    (time.time(), cuenta["id"], cuenta["grupo"], riesgo, RESUMEN[riesgo]))

    # ------------------------------------------------------------ docentes (solo sus grupos)
    def grupos_de(self, docente: str) -> list[str]:
        return [r[0] for r in self._q("SELECT grupo FROM docente_grupos WHERE docente = ?", (docente,))]

    def resumen(self, docente: str, dias: int = 7) -> dict:
        grupos = self.grupos_de(docente)
        desde = time.strftime("%Y-%m-%d", time.localtime(time.time() - dias * 86400))
        out = {}
        for g in grupos:
            nombre, nivel = self._q("SELECT nombre, nivel FROM grupos WHERE id = ?", (g,))[0]
            temas = dict(self._q("SELECT tema, SUM(mensajes) FROM temas WHERE grupo = ? AND dia >= ? GROUP BY tema",
                                 (g, desde)))
            activos = self._q("SELECT COUNT(DISTINCT u.cuenta) FROM uso u JOIN cuentas c ON c.id = u.cuenta "
                              "WHERE c.grupo = ? AND u.dia >= ?", (g, desde))[0][0]
            out[g] = {"nombre": nombre, "nivel": nivel, "temas": temas, "estudiantes_activos": activos}
        return out

    def alertas(self, docente: str) -> list[dict]:
        grupos = self.grupos_de(docente)
        if not grupos:
            return []
        marcas = ",".join("?" * len(grupos))
        filas = self._q(f"SELECT a.id, a.ts, a.cuenta, c.alias, a.grupo, a.categoria, a.resumen, a.atendida FROM "
                        f"alertas a JOIN cuentas c ON c.id = a.cuenta WHERE a.grupo IN ({marcas}) "
                        f"ORDER BY a.ts DESC LIMIT 200", grupos)
        return [dict(zip(("id", "ts", "cuenta", "alias", "grupo", "categoria", "resumen", "atendida"), f))
                for f in filas]

    def marcar_atendida(self, docente: str, alerta: int) -> bool:
        grupos = self.grupos_de(docente)
        fila = self._q("SELECT grupo FROM alertas WHERE id = ?", (alerta,))
        if not fila or fila[0][0] not in grupos:
            return False
        self._q("UPDATE alertas SET atendida = 1 WHERE id = ?", (alerta,))
        return True

    def purgar(self) -> None:
        """Retención mínima: lo viejo se borra solo."""
        ahora = time.time()
        for tabla, col in (("uso", "dia"), ("temas", "dia")):
            limite = time.strftime("%Y-%m-%d", time.localtime(ahora - RETENCION_DIAS[tabla] * 86400))
            self._q(f"DELETE FROM {tabla} WHERE {col} < ?", (limite,))
        self._q("DELETE FROM alertas WHERE ts < ?", (ahora - RETENCION_DIAS["alertas"] * 86400,))
        self._q("DELETE FROM codigos WHERE expira < ?", (ahora,))
