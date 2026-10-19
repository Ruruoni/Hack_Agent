"""Layered persistent memory.

Layers: working (per-conversation/task state, lives in conversations.state / tasks.state),
        episodic, semantic, procedural, preference, project  (rows in `memories`).

Trust model: only a human (`user_verified`) can make a memory trusted. Anything the model proposes is stored as
`unverified` or `hypothesis` and is labelled as such in prompts. Repetition never increases trust.
"""
from __future__ import annotations

from typing import Callable

from ..database.db import Database, dumps, new_id, now
from ..knowledge.store import fts_query, rrf
from ..knowledge.vectorstore import VectorStore
from ..llm.base import LLMError
from ..llm.embeddings import Embedder

LAYERS = ("episodic", "semantic", "procedural", "preference", "project")
KINDS = ("fact", "preference", "correction", "hypothesis", "procedure", "episode", "note")
TRUSTS = ("user_verified", "source_backed", "unverified", "hypothesis")
VEC_COLLECTION = "memories"


class MemoryStore:
    def __init__(self, db: Database, vectors: VectorStore, get_embedder: Callable[[], Embedder], min_similarity: float = 0.15):
        self.db, self.vectors, self.get_embedder = db, vectors, get_embedder
        self.min_similarity = min_similarity

    # ---- CRUD -----------------------------------------------------------------------
    def add(self, layer: str, content: str, *, kind: str = "fact", title: str = "", project_id: str = "default",
            confidence: float = 0.5, trust: str = "unverified", source_type: str = "", source_ref: str = "",
            conversation_id: str | None = None, supersedes: str | None = None, meta: dict | None = None) -> dict:
        if layer not in LAYERS:
            raise ValueError(f"layer must be one of {LAYERS}")
        if trust not in TRUSTS:
            raise ValueError(f"trust must be one of {TRUSTS}")
        content = content.strip()
        if not content:
            raise ValueError("content is empty")
        mid = new_id("mem_")
        t = now()
        with self.db.tx() as c:
            c.execute("INSERT INTO memories(id,layer,kind,project_id,title,content,confidence,trust,status,source_type,"
                      "source_ref,conversation_id,supersedes,meta,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (mid, layer, kind, project_id, title or content[:60], content, confidence, trust, "active",
                       source_type, source_ref, conversation_id, supersedes, dumps(meta or {}), t, t))
            c.execute("INSERT INTO memories_fts(rowid,text) SELECT rowid, title||' '||content FROM memories WHERE id=?", (mid,))
            if supersedes:
                c.execute("UPDATE memories SET status='outdated', updated=? WHERE id=?", (t, supersedes))
        self._index(mid, content)
        return self.db.get("memories", mid)  # type: ignore[return-value]

    def _index(self, mid: str, text: str) -> None:
        try:
            v = self.get_embedder().embed([text])
            self.vectors.upsert(VEC_COLLECTION, [mid], v)
            self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('memory_embedder',?)", (self.get_embedder().id,))
        except LLMError:
            pass  # keyword retrieval still works; reindex() repairs vectors

    def get(self, mid: str) -> dict | None:
        return self.db.get("memories", mid)

    def update(self, mid: str, **changes) -> dict | None:
        allowed = {"title", "content", "confidence", "trust", "kind", "status", "layer", "meta"}
        ch = {k: v for k, v in changes.items() if k in allowed}
        if "trust" in ch and ch["trust"] not in TRUSTS:
            raise ValueError("bad trust")
        ch["updated"] = now()
        self.db.update("memories", mid, ch)
        row = self.get(mid)
        if row and ("content" in ch or "title" in ch):
            with self.db.tx() as c:
                c.execute("DELETE FROM memories_fts WHERE rowid=(SELECT rowid FROM memories WHERE id=?)", (mid,))
                c.execute("INSERT INTO memories_fts(rowid,text) SELECT rowid, title||' '||content FROM memories WHERE id=?", (mid,))
            self._index(mid, row["content"])
        return row

    def mark_outdated(self, mid: str) -> None:
        self.update(mid, status="outdated")

    def delete(self, mid: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM memories_fts WHERE rowid=(SELECT rowid FROM memories WHERE id=?)", (mid,))
            c.execute("DELETE FROM memories WHERE id=?", (mid,))
        self.vectors.delete(VEC_COLLECTION, [mid])
        self.db.audit("user", "memory.delete", {"id": mid})

    def list(self, layer: str | None = None, project_id: str | None = None, status: str | None = "active",
             limit: int = 200, offset: int = 0) -> list[dict]:
        cond, p = [], []
        for col, v in (("layer", layer), ("project_id", project_id), ("status", status)):
            if v:
                cond.append(f"{col}=?")
                p.append(v)
        where = ("WHERE " + " AND ".join(cond)) if cond else ""
        return self.db.query(f"SELECT * FROM memories {where} ORDER BY updated DESC LIMIT ? OFFSET ?", (*p, limit, offset))

    def counts(self) -> dict:
        rows = self.db.query("SELECT layer, COUNT(*) AS n FROM memories WHERE status='active' GROUP BY layer")
        return {r["layer"]: r["n"] for r in rows}

    # ---- retrieval --------------------------------------------------------------------
    def search(self, query: str, *, layers: list[str] | None = None, project_id: str | None = None, k: int = 5,
               conversation_id: str | None = None, include_outdated: bool = False) -> list[dict]:
        """Hybrid search; verified memories get a small boost, learned utility nudges ranking."""
        vec_rank: list[str] = []
        try:
            emb = self.get_embedder()
            stored = self.db.scalar("SELECT value FROM meta WHERE key='memory_embedder'", (), emb.id)
            if stored == emb.id:
                vec_rank = [i for i, sim in self.vectors.query(VEC_COLLECTION, emb.embed([query])[0], k * 4) if sim >= self.min_similarity]
        except LLMError:
            pass
        kw_rank: list[str] = []
        fq = fts_query(query)
        if fq:
            kw_rank = [r["id"] for r in self.db.query(
                "SELECT m.id FROM memories_fts f JOIN memories m ON m.rowid=f.rowid WHERE memories_fts MATCH ? "
                "ORDER BY bm25(memories_fts) LIMIT ?", (fq, k * 4))]
        fused = rrf([vec_rank, kw_rank])
        if not fused:
            return []
        ph = ",".join("?" * len(fused))
        rows = {r["id"]: r for r in self.db.query(f"SELECT * FROM memories WHERE id IN ({ph})", list(fused))}
        out = []
        for id_, sc in fused.items():
            r = rows.get(id_)
            if not r or (r["status"] != "active" and not include_outdated):
                continue
            if layers and r["layer"] not in layers:
                continue
            if project_id and r["project_id"] not in (project_id, "default", "*"):
                continue
            # one-time corrections only apply inside the conversation they were made in
            if r["kind"] == "correction" and (r["meta"] or {}).get("scope") == "one_time" and r["conversation_id"] != conversation_id:
                continue
            boost = 1.15 if r["trust"] == "user_verified" else 1.0
            boost *= 1.0 + max(-0.5, min(0.5, 0.1 * (r["utility"] or 0)))
            r["score"] = round(sc * boost, 5)
            out.append(r)
        out.sort(key=lambda r: -r["score"])
        return out[:k]

    def preferences(self, project_id: str = "default", limit: int = 20) -> list[dict]:
        return self.db.query("SELECT * FROM memories WHERE layer='preference' AND status='active' AND "
                             "(project_id=? OR project_id='default') ORDER BY updated DESC LIMIT ?", (project_id, limit))

    def touch(self, ids: list[str], delta: float = 0.0) -> None:
        if not ids:
            return
        with self.db.conn() as c:
            c.executemany("UPDATE memories SET uses=uses+1, utility=MAX(-5,MIN(5,COALESCE(utility,0)+?)) WHERE id=?",
                          [(delta, i) for i in ids])

    # ---- maintenance ------------------------------------------------------------------
    def reindex(self) -> int:
        emb = self.get_embedder()
        self.vectors.drop(VEC_COLLECTION)
        n = 0
        last = ""
        while True:
            rows = self.db.query("SELECT id, title, content FROM memories WHERE id>? ORDER BY id LIMIT 200", (last,))
            if not rows:
                break
            self.vectors.upsert(VEC_COLLECTION, [r["id"] for r in rows], emb.embed([f"{r['title']} {r['content']}" for r in rows]))
            n += len(rows)
            last = rows[-1]["id"]
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('memory_embedder',?)", (emb.id,))
        return n

    def export(self) -> list[dict]:
        return self.db.query("SELECT * FROM memories ORDER BY created")
