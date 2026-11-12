"""Emergencias: TISMA (el proveedor) revisa la conversación SOLO cuando hay señales de riesgo grave, para poder
avisar a las autoridades si un estudiante está en peligro.

- Solo autolesión y abuso. En todo lo demás el texto no se guarda nunca (ver datos.py).
- La conversación se guarda SELLADA: cifrada con la clave pública del equipo de seguridad de TISMA (X25519 + AES-256-GCM).
  El servidor puede sellar pero no abrir: la clave privada no está en el servidor. Si alguien roba el servidor o la
  base de datos, no hay nada legible.
- El aviso al equipo (webhook) dice «emergencia N.º X, categoría, institución»: nunca el contenido ni el alias.
- Abrir una emergencia, y decidir, queda en una auditoría: quién, cuándo y qué decidió (falso positivo, se avisó a
  la escuela o se avisó a las autoridades).
- El contenido sellado se borra al resolverla o a los 30 días como mucho; la auditoría se conserva (rendición de
  cuentas), sin contenido.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CATEGORIAS = {"autolesion", "abuso"}
DECISIONES = {"falso_positivo", "escuela", "autoridades"}
VIGENCIA_DIAS = 30
MAX_MENSAJES = 12
INFO = b"tisma-ia-escolar emergencia v1"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS emergencias (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    cuenta TEXT NOT NULL, institucion TEXT NOT NULL, grupo TEXT, categoria TEXT NOT NULL, sello BLOB,
    estado TEXT NOT NULL DEFAULT 'pendiente', decidido_por TEXT, decidido_ts REAL, nota TEXT);
CREATE TABLE IF NOT EXISTS auditoria (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, revisor TEXT NOT NULL,
    accion TEXT NOT NULL, emergencia INTEGER NOT NULL);
"""


# ---------------------------------------------------------------- cifrado (sellar con la pública, abrir con la privada)
def _clave(compartido: bytes, efimera: bytes, destino: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=efimera + destino, info=INFO).derive(compartido)


def generar_claves() -> tuple[bytes, bytes]:
    """(privada, pública) en bytes crudos de 32."""
    priv = X25519PrivateKey.generate()
    return (priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                               serialization.NoEncryption()),
            priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def sellar(publica: bytes, datos: bytes, asociado: bytes = b"") -> bytes:
    efimera = X25519PrivateKey.generate()
    ef_pub = efimera.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    clave = _clave(efimera.exchange(X25519PublicKey.from_public_bytes(publica)), ef_pub, publica)
    nonce = os.urandom(12)
    return ef_pub + nonce + AESGCM(clave).encrypt(nonce, datos, asociado)


def abrir(privada: bytes, sello: bytes, asociado: bytes = b"") -> bytes:
    priv = X25519PrivateKey.from_private_bytes(privada)
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ef_pub, nonce, ct = sello[:32], sello[32:44], sello[44:]
    clave = _clave(priv.exchange(X25519PublicKey.from_public_bytes(ef_pub)), ef_pub, pub)
    return AESGCM(clave).decrypt(nonce, ct, asociado)


def _asociado(emergencia_ts: float, cuenta: str) -> bytes:
    """Ata el sello a su emergencia: no se puede copiar el contenido de una a otra."""
    return f"{emergencia_ts:.6f}|{cuenta}".encode()


# ---------------------------------------------------------------- registro y aviso (en el servidor)
class Emergencias:
    def __init__(self, datos, publica: bytes, webhook: str | None = None, enviar=None):
        if len(publica) != 32:
            raise ValueError("la clave pública del equipo de seguridad debe ser X25519 (32 bytes)")
        self.datos, self.publica, self.webhook = datos, publica, webhook
        self.enviar = enviar or self._post
        with datos.lock:
            datos.db.executescript(ESQUEMA)

    def registrar(self, cuenta: dict, categoria: str, mensajes: list[dict], respuesta: str) -> int | None:
        if categoria not in CATEGORIAS:
            return None
        ts = time.time()
        contenido = json.dumps({"mensajes": mensajes[-MAX_MENSAJES:], "respuesta": respuesta,
                                "alias": self._alias(cuenta["id"])}, ensure_ascii=False).encode()
        sello = sellar(self.publica, contenido, _asociado(ts, cuenta["id"]))
        with self.datos.lock:
            cur = self.datos.db.execute(
                "INSERT INTO emergencias (ts, cuenta, institucion, grupo, categoria, sello) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, cuenta["id"], cuenta["institucion"], cuenta["grupo"], categoria, sello))
            eid = cur.lastrowid
        aviso = {"emergencia": eid, "categoria": categoria, "institucion": cuenta["institucion"],
                 "ts": int(ts)}                         # nunca el contenido ni el alias
        threading.Thread(target=self.enviar, args=(aviso,), daemon=True).start()
        return eid

    def _alias(self, cuenta: str) -> str | None:
        fila = self.datos._q("SELECT alias FROM cuentas WHERE id = ?", (cuenta,))
        return fila[0][0] if fila else None

    def _post(self, aviso: dict) -> None:
        if not self.webhook:
            return
        try:
            req = urllib.request.Request(self.webhook, data=json.dumps(aviso).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).close()  # noqa: S310 (URL configurada por TISMA)
        except OSError:
            pass                                         # la emergencia queda pendiente igual: el equipo la ve al listar

    def purgar(self) -> None:
        limite = time.time() - VIGENCIA_DIAS * 86400
        self.datos._q("UPDATE emergencias SET sello = NULL WHERE ts < ?", (limite,))


# ---------------------------------------------------------------- consola del equipo de seguridad
class Consola:
    """La usa una persona del equipo de seguridad de TISMA, con la clave privada que NO vive en el servidor."""

    def __init__(self, datos, revisor: str):
        if not revisor.strip():
            raise ValueError("hace falta identificar a quien revisa")
        self.datos, self.revisor = datos, revisor.strip()[:60]
        with datos.lock:
            datos.db.executescript(ESQUEMA)

    def _auditar(self, accion: str, eid: int) -> None:
        self.datos._q("INSERT INTO auditoria (ts, revisor, accion, emergencia) VALUES (?, ?, ?, ?)",
                      (time.time(), self.revisor, accion, eid))

    def pendientes(self) -> list[dict]:
        filas = self.datos._q("SELECT e.id, e.ts, e.categoria, i.nombre, g.nombre FROM emergencias e "
                              "JOIN instituciones i ON i.id = e.institucion LEFT JOIN grupos g ON g.id = e.grupo "
                              "WHERE e.estado = 'pendiente' ORDER BY e.ts")
        return [dict(zip(("id", "ts", "categoria", "institucion", "grupo"), f)) for f in filas]

    def ver(self, eid: int, privada: bytes) -> dict:
        fila = self.datos._q("SELECT ts, cuenta, sello FROM emergencias WHERE id = ?", (eid,))
        if not fila:
            raise KeyError("no existe")
        ts, cuenta, sello = fila[0]
        self._auditar("ver", eid)                        # se registra ANTES de mostrar nada
        if sello is None:
            raise KeyError("el contenido ya se borró (resuelta o vencida)")
        return json.loads(abrir(privada, sello, _asociado(ts, cuenta)))

    def resolver(self, eid: int, decision: str, nota: str = "") -> None:
        if decision not in DECISIONES:
            raise ValueError(f"decisión: {', '.join(sorted(DECISIONES))}")
        if not self.datos._q("SELECT 1 FROM emergencias WHERE id = ? AND estado = 'pendiente'", (eid,)):
            raise KeyError("no existe o ya se resolvió")
        self.datos._q("UPDATE emergencias SET estado = ?, decidido_por = ?, decidido_ts = ?, nota = ?, sello = NULL "
                      "WHERE id = ?", (decision, self.revisor, time.time(), nota[:500], eid))
        self._auditar(f"resolver:{decision}", eid)

    def auditoria(self, eid: int) -> list[tuple]:
        return self.datos._q("SELECT ts, revisor, accion FROM auditoria WHERE emergencia = ? ORDER BY id", (eid,))
