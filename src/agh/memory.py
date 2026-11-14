"""Long-term memory: per-agent SQLite FTS5 store with optional embedding rerank, plus core.md.

Kinds: semantic (facts), episodic (what happened in runs), procedural (how to do things).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KINDS = ("semantic", "episodic", "procedural")
CORE_MAX_CHARS = 4000            # core.md is in every prompt, so it has to stay small
SEMANTIC_SCAN_LIMIT = 2000       # most recent memories compared by embedding when no keyword matches them
SEMANTIC_MIN_SIMILARITY = 0.35   # below this, a memory with no keyword match is noise


@dataclass
class MemoryItem:
    id: int
    kind: str
    text: str
    tags: str
    importance: float
    created: float
    score: float = 0.0


def _fts_query(query: str) -> str:
    """Quote every term so user text can't be read as FTS5 syntax; any term may match."""
    terms = re.findall(r"\w+", query.lower())
    return " OR ".join(f'"{t}"' for t in terms)


class MemoryStore:
    def __init__(self, directory: Path, embedder: Any = None):
        """`directory` holds core.md and memory.db: Workspace.memory_dir(agent) for the active profile."""
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self.db = sqlite3.connect(self.dir / "memory.db", check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS items(
              id INTEGER PRIMARY KEY, kind TEXT, text TEXT, tags TEXT DEFAULT '',
              importance REAL DEFAULT 0.5, created REAL, source TEXT DEFAULT '', vector TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(text, tags, content='items', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
              INSERT INTO items_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags); END;
            CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
              INSERT INTO items_fts(items_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags); END;
        """)

    def close(self) -> None:
        self.db.close()

    def core(self) -> str:
        path = self.dir / "core.md"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def pin(self, text: str) -> bool:
        """Append a one-line note to core.md. Returns False if it is already there."""
        note = "- " + " ".join(text.split())
        path = self.dir / "core.md"
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if note in current.splitlines():
            return False
        if len(current) + len(note) > CORE_MAX_CHARS:
            raise ValueError(f"core memory is full ({CORE_MAX_CHARS} characters); ask the user to prune {path.name}")
        path.write_text(f"{current.rstrip()}\n{note}\n".lstrip(), encoding="utf-8")
        return True

    def add(self, text: str, kind: str = "semantic", tags: str = "", importance: float = 0.5,
            source: str = "") -> int:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        existing = self.db.execute("SELECT id FROM items WHERE text=? AND kind=?", (text, kind)).fetchone()
        if existing:
            return existing[0]
        vector = json.dumps(self.embedder.embed_query(text)) if self.embedder else None
        cur = self.db.execute(
            "INSERT INTO items(kind,text,tags,importance,created,source,vector) VALUES(?,?,?,?,?,?,?)",
            (kind, text, tags, max(0.0, min(1.0, importance)), time.time(), source, vector))
        self.db.commit()
        return int(cur.lastrowid)

    def forget(self, item_id: int) -> bool:
        cur = self.db.execute("DELETE FROM items WHERE id=?", (item_id,))
        self.db.commit()
        return cur.rowcount > 0

    def all(self, kind: str | None = None, limit: int = 100) -> list[MemoryItem]:
        where, args = ("WHERE kind=?", (kind,)) if kind else ("", ())
        rows = self.db.execute(
            f"SELECT id,kind,text,tags,importance,created FROM items {where} ORDER BY created DESC, id DESC LIMIT ?",
            (*args, limit)).fetchall()
        return [MemoryItem(*r) for r in rows]

    def search(self, query: str, k: int = 5, kind: str | None = None,
               kinds: Any = None) -> list[MemoryItem]:
        """Rank keyword hits by BM25, importance and recency. With embeddings, similarity adds to the score
        and also finds memories that share no words with the query. `kind` or `kinds` limits what is searched."""
        fts = _fts_query(query)
        if not fts and not self.embedder:
            return []
        wanted = [kind] if kind else [x for x in (kinds or ()) if x in KINDS]
        filter_sql = f" AND i.kind IN ({','.join('?' * len(wanted))})" if wanted else ""
        filter_args = tuple(wanted)
        rows: list[tuple[Any, ...]] = []
        if fts:
            sql = ("SELECT i.id,i.kind,i.text,i.tags,i.importance,i.created,bm25(items_fts),i.vector "
                   f"FROM items_fts JOIN items i ON i.id=items_fts.rowid WHERE items_fts MATCH ?{filter_sql} "
                   "ORDER BY bm25(items_fts) LIMIT ?")
            rows = self.db.execute(sql, (fts, *filter_args, max(k * 4, 20))).fetchall()
        qvec = self.embedder.embed_query(query) if self.embedder else None
        if qvec:
            keyword_hits = {r[0] for r in rows}
            sql = ("SELECT i.id,i.kind,i.text,i.tags,i.importance,i.created,0.0,i.vector FROM items i "
                   f"WHERE i.vector IS NOT NULL{filter_sql} ORDER BY i.created DESC LIMIT ?")
            rows += [r for r in self.db.execute(sql, (*filter_args, SEMANTIC_SCAN_LIMIT)).fetchall()
                     if r[0] not in keyword_hits and _cosine(qvec, json.loads(r[7])) >= SEMANTIC_MIN_SIMILARITY]
        now = time.time()
        items = []
        for r in rows:
            item = MemoryItem(*r[:6])
            age_days = (now - item.created) / 86400
            item.score = -r[6] + item.importance + 0.5 * math.exp(-age_days / 30)
            if qvec and r[7]:
                item.score += 3 * _cosine(qvec, json.loads(r[7]))
            items.append(item)
        return sorted(items, key=lambda i: i.score, reverse=True)[:k]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def make_embedder(config: dict[str, Any]) -> Any:
    """`memory: {embeddings: "openai:text-embedding-3-small"}` in agh.yaml turns on hybrid search."""
    name = (config.get("memory") or {}).get("embeddings")
    if not name:
        return None
    if name.startswith("azure_openai:"):
        from agh.llm import make_azure_embeddings

        return make_azure_embeddings(name.split(":", 1)[1])
    if name.startswith("openai:"):
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(model=name.split(":", 1)[1])
    try:  # any other LangChain provider, once its packages are installed
        from langchain.embeddings import init_embeddings
    except ImportError:
        raise ValueError(f"memory.embeddings {name!r}: use openai:<model> or azure_openai:<deployment>, or "
                         f"pip install langchain and that provider's package") from None
    return init_embeddings(name)
