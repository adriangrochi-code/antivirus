"""Memoria del arnés, adaptada de Hope (hope/memory/knowledge.py y PESAM).

* **Soluciones verificadas**: tarea + resumen + archivos, solo si ``verify`` pasó (RESPALDADO);
  sin verificación quedan como INFERIDO y pesan menos al recuperar.
* **Lecciones**: el error que mostró la verificación y lo que lo arregló. Es lo que más
  ayuda al reanalizar: dice qué **no** hacer.
* **Contexto**: antes de una tarea se recuperan las soluciones y lecciones más parecidas (FTS5/BM25).
  Se agregan al mensaje del usuario, no al de sistema, para no invalidar la caché KV.
* **Sin duplicados**: una solución casi igual se refuerza (usos + confianza) en vez de repetirse.
* **Utilidad medida**: lo recuperado que acabó en una tarea verificada gana confianza; lo que no, la pierde.
* **PESAM**: registro de experiencias de solo agregado (auditoría).
* **Episodios**: trayectorias verificadas en JSONL para afinar (LoRA) el modelo local más adelante.
  Sin verificación no se guarda nada para entrenar: el modelo no es la autoridad.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

DATA = Path.home() / ".local/share/tisma-code"
RESPALDADO, INFERIDO = "respaldado", "inferido"


def _project_id(root: Path) -> str:
    return hashlib.sha1(str(root).encode()).hexdigest()[:12]


def _fts_query(text: str) -> str:
    words = [w for w in re.findall(r"\w{3,}", text.lower())][:24]
    return " OR ".join(f'"{w}"' for w in words)


class KnowledgeBase:
    def __init__(self, root: Path, base: Path = DATA):
        self.root = root
        self.dir = base / "proyectos" / _project_id(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "conocimiento.db", check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS item(
                id INTEGER PRIMARY KEY, kind TEXT, task TEXT, body TEXT, status TEXT,
                confidence REAL DEFAULT 0.5, uses INTEGER DEFAULT 0, created REAL);
            CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5(task, body, content='item', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS item_ai AFTER INSERT ON item BEGIN
                INSERT INTO item_fts(rowid, task, body) VALUES (new.id, new.task, new.body); END;
            CREATE TABLE IF NOT EXISTS model_stats(model TEXT PRIMARY KEY, tries INTEGER, wins INTEGER);
        """)
        (self.dir / "proyecto.txt").write_text(str(root))

    # ------------------------------------------------------------ aprender
    def _similar(self, kind: str, text: str, k: int = 5) -> list[tuple]:
        q = _fts_query(text)
        if not q:
            return []
        return self.db.execute(
            "SELECT item.id, item.task, item.body, item.status, item.confidence, bm25(item_fts) AS s "
            "FROM item_fts JOIN item ON item.id = item_fts.rowid "
            "WHERE item_fts MATCH ? AND item.kind = ? AND item.status != 'refutado' ORDER BY s LIMIT ?",
            (q, kind, k)).fetchall()

    def learn_solution(self, task: str, summary: str, files: list[str], verified: bool) -> int:
        body = summary.strip()[:2000] + ("\nArchivos: " + ", ".join(files[:20]) if files else "")
        for row in self._similar("solucion", task, 1):
            if row[1].strip().lower() == task.strip().lower():
                self.db.execute("UPDATE item SET uses = uses + 1, confidence = min(1, confidence + 0.1), "
                                "body = ?, status = ? WHERE id = ?",
                                (body, RESPALDADO if verified else row[3], row[0]))
                self.db.commit()
                return row[0]
        cur = self.db.execute("INSERT INTO item(kind, task, body, status, confidence, created) VALUES "
                              "('solucion', ?, ?, ?, ?, ?)",
                              (task[:1000], body, RESPALDADO if verified else INFERIDO,
                               0.7 if verified else 0.3, time.time()))
        self.db.commit()
        return cur.lastrowid

    def learn_lesson(self, task: str, failure: str, fix: str) -> int:
        body = f"Fallo: {failure.strip()[-800:]}\nLo que lo arregló: {fix.strip()[:800]}"
        cur = self.db.execute("INSERT INTO item(kind, task, body, status, confidence, created) VALUES "
                              "('leccion', ?, ?, ?, 0.6, ?)", (task[:1000], body, RESPALDADO, time.time()))
        self.db.commit()
        return cur.lastrowid

    def feedback(self, ids: list[int], helped: bool) -> None:
        delta = 0.1 if helped else -0.1
        for i in ids:
            self.db.execute("UPDATE item SET uses = uses + 1, confidence = max(0, min(1, confidence + ?)) "
                            "WHERE id = ?", (delta, i))
        self.db.commit()

    # ------------------------------------------------------------ recuperar
    def context(self, task: str, k: int = 3) -> tuple[str, list[int]]:
        rows = [r for r in self._similar("leccion", task, k) + self._similar("solucion", task, k)
                if r[4] >= 0.3]
        if not rows:
            return "", []
        parts = ["Experiencia previa en este proyecto (úsala si aplica, no la copies a ciegas):"]
        for r in rows:
            tag = "LECCIÓN" if r[2].startswith("Fallo:") else f"SOLUCIÓN {r[3]}"
            parts.append(f"- [{tag}] Tarea: {r[1][:200]}\n  {r[2][:600]}")
        return "\n".join(parts), [r[0] for r in rows]

    # ------------------------------------------------------------ modelos (UCB1, del consejo de Hope)
    def record_model(self, model: str, won: bool) -> None:
        self.db.execute("INSERT INTO model_stats VALUES (?, 1, ?) ON CONFLICT(model) DO UPDATE SET "
                        "tries = tries + 1, wins = wins + ?", (model, int(won), int(won)))
        self.db.commit()

    def model_stats(self) -> dict[str, tuple[int, int]]:
        return {m: (t, w) for m, t, w in self.db.execute("SELECT model, tries, wins FROM model_stats")}

    def stats(self) -> dict:
        rows = self.db.execute("SELECT kind, status, count(*) FROM item GROUP BY kind, status").fetchall()
        return {f"{k}:{s}": n for k, s, n in rows}

    # ------------------------------------------------------------ PESAM y episodios
    def log(self, event: dict) -> None:
        with open(self.dir / "pesam.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), **event}, ensure_ascii=False) + "\n")

    def save_episode(self, task: str, messages: list[dict], model: str) -> Path:
        """Trayectoria verificada en formato de chat (apto para afinar con LoRA)."""
        p = self.dir / "episodios.jsonl"
        clean = [{k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "tool_call_id")}
                 for m in messages]
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"task": task, "model": model, "verified": True, "messages": clean},
                                ensure_ascii=False) + "\n")
        return p
