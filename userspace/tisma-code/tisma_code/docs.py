"""Espacio de documentos locales: biblioteca consultable + memoria escrita por el agente.

* ``~/TISMA/Documentos``: biblioteca del usuario (manuales, hojas de datos, apuntes…).
* ``<proyecto>/.tisma``: descubrimientos y mejoras que el agente guarda en Markdown.

Ambos se indexan con SQLite FTS5 (BM25): buscar es instantáneo incluso con miles de
documentos, y el índice se actualiza solo con los archivos nuevos o modificados.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

LIBRARY = Path(os.environ.get("TISMA_DOCS", Path.home() / "TISMA" / "Documentos"))
TEXT_EXT = {".md", ".txt", ".rst", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".py", ".c", ".h",
            ".cpp", ".hpp", ".rs", ".go", ".js", ".ts", ".java", ".kt", ".sh", ".gcode", ".tex", ".html"}
CHUNK = 1800            # caracteres por fragmento indexado
MAX_FILE = 20_000_000
KINDS = {"descubrimiento": "descubrimientos", "mejora": "mejoras", "nota": "notas"}


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower(), flags=re.UNICODE).strip()
    return re.sub(r"[\s_-]+", "-", s)[:60] or "nota"


def _extract(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        if not shutil.which("pdftotext"):
            return ""
        r = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True,
                           timeout=60, errors="replace")
        return r.stdout
    return path.read_text(encoding="utf-8", errors="replace")


STOP = {"de", "del", "la", "el", "los", "las", "un", "una", "y", "o", "en", "por", "para", "con", "que",
        "es", "se", "al", "lo", "su", "the", "of", "and", "to", "in", "is", "for", "on", "a"}


def _fts_query(text: str) -> str:
    words = [w for w in re.findall(r"\w{2,}", text.lower()) if w not in STOP][:20]
    return " OR ".join(f'"{w}"*' for w in words)


class DocSpace:
    def __init__(self, project: Path, index_dir: Path, library: Path = LIBRARY):
        self.project_dir = project / ".tisma"
        self.library = library
        for d in [self.library, *(self.project_dir / k for k in KINDS.values())]:
            d.mkdir(parents=True, exist_ok=True)
        readme = self.project_dir / "LEEME.md"
        if not readme.exists():
            readme.write_text("# Memoria del proyecto (TISMA Code)\n\nAquí el agente guarda lo que descubre "
                              "(`descubrimientos/`) y las mejoras verificadas (`mejoras/`). Puedes editar o "
                              "borrar cualquier nota; el índice se actualiza solo.\n", encoding="utf-8")
        index_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(index_dir / "documentos.db", check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS doc(path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk USING fts5(path UNINDEXED, title, body, n UNINDEXED);
        """)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ índice
    def roots(self) -> list[tuple[str, Path]]:
        return [("biblioteca", self.library), ("proyecto", self.project_dir)]

    def refresh(self) -> int:
        """Indexa lo nuevo o modificado y olvida lo borrado. Devuelve cuántos archivos reindexó."""
        seen, changed = set(), 0
        with self._lock:
            known = {p: (m, s) for p, m, s in self.db.execute("SELECT path, mtime, size FROM doc")}
            for _, root in self.roots():
                for dirpath, dirnames, files in os.walk(root):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for f in files:
                        p = Path(dirpath) / f
                        if p.suffix.lower() not in TEXT_EXT | {".pdf"}:
                            continue
                        try:
                            st = p.stat()
                        except OSError:
                            continue
                        if st.st_size > MAX_FILE:
                            continue
                        key = str(p)
                        seen.add(key)
                        if known.get(key) == (st.st_mtime, st.st_size):
                            continue
                        self._index(p, st)
                        changed += 1
            for gone in set(known) - seen:
                self.db.execute("DELETE FROM doc WHERE path = ?", (gone,))
                self.db.execute("DELETE FROM chunk WHERE path = ?", (gone,))
            self.db.commit()
        return changed

    def _index(self, p: Path, st) -> None:
        try:
            text = _extract(p)
        except (OSError, subprocess.SubprocessError):
            text = ""
        key = str(p)
        self.db.execute("DELETE FROM chunk WHERE path = ?", (key,))
        title = p.stem
        m = re.search(r"^#\s+(.+)$", text, re.M)
        if m:
            title = m.group(1).strip()
        # Fragmentos por párrafos para devolver solo la parte relevante
        buf, n = "", 0
        for para in re.split(r"\n\s*\n", text):
            if len(buf) + len(para) > CHUNK and buf:
                self.db.execute("INSERT INTO chunk VALUES (?, ?, ?, ?)", (key, title, buf, n))
                buf, n = "", n + 1
            buf += para + "\n\n"
        if buf.strip():
            self.db.execute("INSERT INTO chunk VALUES (?, ?, ?, ?)", (key, title, buf, n))
        self.db.execute("INSERT OR REPLACE INTO doc VALUES (?, ?, ?)", (key, st.st_mtime, st.st_size))

    # ------------------------------------------------------------ uso
    def where(self, path: str) -> str:
        for name, root in self.roots():
            if path.startswith(str(root)):
                return f"{name}:{Path(path).relative_to(root)}"
        return path

    def search(self, query: str, k: int = 6) -> list[dict]:
        self.refresh()
        q = _fts_query(query)
        if not q:
            return []
        rows = self.db.execute(
            "SELECT path, title, snippet(chunk, 2, '«', '»', ' … ', 40), n, bm25(chunk, 0, 3.0, 1.0) AS s "
            "FROM chunk WHERE chunk MATCH ? ORDER BY s LIMIT ?", (q, k)).fetchall()
        return [{"ref": self.where(p), "title": t, "snippet": sn, "part": n} for p, t, sn, n, _ in rows]

    def resolve(self, ref: str) -> Path:
        name, _, rel = ref.partition(":")
        root = dict(self.roots()).get(name)
        if root is None or not rel:
            raise ValueError("Referencia inválida: usa «biblioteca:ruta» o «proyecto:ruta»")
        p = (root / rel).resolve()
        if root.resolve() not in p.parents:
            raise ValueError("Ruta fuera del espacio de documentos")
        return p

    def read(self, ref: str, part: int | None = None, limit: int = 12000) -> str:
        p = self.resolve(ref)
        if part is not None:
            row = self.db.execute("SELECT body FROM chunk WHERE path = ? AND n = ?", (str(p), part)).fetchone()
            if row:
                return row[0]
        text = _extract(p)
        return text[:limit] + (f"\n… ({len(text)} caracteres; pide otra parte con part)" if len(text) > limit else "")

    def save(self, kind: str, title: str, content: str, tags: list[str] | None = None) -> str:
        if kind not in KINDS:
            raise ValueError(f"Tipo inválido: {kind} (descubrimiento, mejora o nota)")
        today = _dt.date.today().isoformat()
        p = self.project_dir / KINDS[kind] / f"{today}-{_slug(title)}.md"
        i = 2
        while p.exists():
            p = p.with_name(f"{today}-{_slug(title)}-{i}.md")
            i += 1
        head = f"# {title.strip()}\n\n- Tipo: {kind}\n- Fecha: {today}\n"
        if tags:
            head += f"- Etiquetas: {', '.join(tags)}\n"
        p.write_text(head + "\n" + content.strip() + "\n", encoding="utf-8")
        self.refresh()
        return self.where(str(p))

    def recent(self, n: int = 12) -> list[dict]:
        self.refresh()
        out = []
        for path, mtime, _ in self.db.execute("SELECT path, mtime, size FROM doc ORDER BY mtime DESC LIMIT ?", (n,)):
            out.append({"ref": self.where(path), "name": Path(path).name, "mtime": mtime})
        return out


SCHEMAS = [
    {"type": "function", "function": {
        "name": "docs_buscar", "description": "Busca en los documentos locales (biblioteca del usuario y memoria del "
        "proyecto: descubrimientos y mejoras anteriores). Úsalo antes de investigar algo desde cero.",
        "parameters": {"type": "object", "required": ["consulta"], "properties": {"consulta": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "docs_leer", "description": "Lee un documento local por su referencia (de docs_buscar).",
        "parameters": {"type": "object", "required": ["ref"], "properties": {
            "ref": {"type": "string", "description": "p. ej. biblioteca:manuales/motor.pdf"},
            "part": {"type": "integer", "description": "fragmento concreto (opcional)"}}}}},
    {"type": "function", "function": {
        "name": "docs_guardar", "description": "Guarda en la memoria del proyecto algo que vale la pena recordar: "
        "un descubrimiento (cómo funciona algo, causa de un error) o una mejora (cambio hecho y por qué).",
        "parameters": {"type": "object", "required": ["tipo", "titulo", "contenido"], "properties": {
            "tipo": {"type": "string", "enum": ["descubrimiento", "mejora", "nota"]},
            "titulo": {"type": "string"}, "contenido": {"type": "string"},
            "etiquetas": {"type": "array", "items": {"type": "string"}}}}}},
]
