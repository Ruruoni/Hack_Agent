"""Knowledge base: collections, documents, chunks, hybrid (vector + keyword) retrieval."""
from __future__ import annotations

import logging
import re
from typing import Callable

import numpy as np

from ..database.db import Database, new_id, now
from ..llm.base import LLMError
from ..llm.embeddings import Embedder
from .text import bands, hamming, to_signed, to_unsigned
from .vectorstore import VectorStore

log = logging.getLogger("nexus.kb")
_TOK = re.compile(r"[A-Za-z0-9_]{2,}")


def fts_query(q: str) -> str:
    toks = list(dict.fromkeys(t.lower() for t in _TOK.findall(q)))[:24]
    return " OR ".join(f'"{t}"' for t in toks)


def rrf(rank_lists: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for lst in rank_lists:
        for r, id_ in enumerate(lst):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (k + r + 1)
    return scores


class KnowledgeBase:
    def __init__(self, db: Database, vectors: VectorStore, get_embedder: Callable[[], Embedder], min_similarity: float = 0.15):
        self.db, self.vectors, self.get_embedder = db, vectors, get_embedder
        self.min_similarity = min_similarity

    # ---- collections ---------------------------------------------------------------
    def create_collection(self, name: str, description: str = "", project_id: str = "default") -> dict:
        ex = self.db.one("SELECT * FROM collections WHERE name=? AND project_id=?", (name, project_id))
        if ex:
            return ex
        cid = new_id("col_")
        self.db.insert("collections", {"id": cid, "name": name, "description": description, "project_id": project_id,
                                       "embedder": "", "dim": 0, "created": now(), "updated": now()})
        return self.db.get("collections", cid)  # type: ignore[return-value]

    def get_collection(self, ref: str) -> dict | None:
        return self.db.one("SELECT * FROM collections WHERE id=? OR name=?", (ref, ref))

    def list_collections(self, project_id: str | None = None) -> list[dict]:
        sql = ("SELECT c.*, (SELECT COUNT(*) FROM documents d WHERE d.collection_id=c.id) AS documents, "
               "(SELECT COUNT(*) FROM chunks k WHERE k.collection_id=c.id AND k.status='indexed') AS chunks, "
               "(SELECT COUNT(*) FROM chunks k WHERE k.collection_id=c.id AND k.status='quarantined') AS quarantined "
               "FROM collections c")
        rows = self.db.query(sql + (" WHERE project_id=?" if project_id else "") + " ORDER BY created", (project_id,) if project_id else ())
        cur = self.get_embedder().id
        for r in rows:
            r["needs_reindex"] = bool(r["chunks"]) and r["embedder"] not in ("", cur)
        return rows

    def delete_collection(self, cid: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE collection_id=?)", (cid,))
            c.execute("DELETE FROM chunks WHERE collection_id=?", (cid,))
            c.execute("DELETE FROM facts WHERE collection_id=?", (cid,))
            c.execute("DELETE FROM documents WHERE collection_id=?", (cid,))
            c.execute("DELETE FROM collections WHERE id=?", (cid,))
        self.vectors.drop(cid)
        self.db.audit("system", "collection.delete", {"id": cid})

    def delete_document(self, doc_id: str) -> None:
        d = self.db.get("documents", doc_id)
        if not d:
            return
        ids = [r["id"] for r in self.db.query("SELECT id FROM chunks WHERE document_id=?", (doc_id,))]
        with self.db.tx() as c:
            c.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE document_id=?)", (doc_id,))
            c.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
            c.execute("DELETE FROM facts WHERE document_id=?", (doc_id,))
            c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        self.vectors.delete(d["collection_id"], ids)

    def bind_embedder(self, cid: str, dim: int) -> None:
        emb = self.get_embedder()
        self.db.update("collections", cid, {"embedder": emb.id, "dim": dim, "updated": now()})

    # ---- dedup helpers (used by ingestion) -----------------------------------------------
    def find_exact(self, conn, collection_id: str, sha: str) -> str | None:
        r = conn.execute("SELECT id FROM chunks WHERE collection_id=? AND sha=? LIMIT 1", (collection_id, sha)).fetchone()
        return r[0] if r else None

    def find_near(self, conn, collection_id: str, simhash: int, max_dist: int) -> str | None:
        b = bands(simhash)
        rows = conn.execute(
            "SELECT id, simhash FROM chunks WHERE collection_id=? AND (b0=? OR b1=? OR b2=? OR b3=?) LIMIT 50",
            (collection_id, *b)).fetchall()
        for r in rows:
            if hamming(to_unsigned(r[1]), simhash) <= max_dist:
                return r[0]
        return None

    # ---- retrieval -----------------------------------------------------------------------
    def search(self, query: str, collection_ids: list[str] | None = None, k: int = 5,
               project_id: str | None = None, use_vectors: bool = True) -> dict:
        """Hybrid search. Returns {hits:[...], degraded: str|None}. Never raises on embedder trouble."""
        if collection_ids is None:
            rows = self.db.query("SELECT id, embedder FROM collections" + (" WHERE project_id=?" if project_id else ""),
                                 (project_id,) if project_id else ())
        else:
            rows = [r for r in (self.get_collection(c) for c in collection_ids) if r]
        if not rows or not query.strip():
            return {"hits": [], "degraded": None}
        cids = [r["id"] for r in rows]
        degraded = None
        vec_rank: list[str] = []
        sims: dict[str, float] = {}
        if use_vectors:
            try:
                emb = self.get_embedder()
                qv = emb.embed([query])[0]
                for r in rows:
                    if r["embedder"] in ("", emb.id):
                        for id_, s in self.vectors.query(r["id"], qv, k * 3):
                            sims[id_] = s
                    else:
                        degraded = f"Collection built with '{r['embedder']}' but current embedder is '{emb.id}': keyword search only. Re-index to fix."
                vec_rank = [i for i, _ in sorted(sims.items(), key=lambda x: -x[1])]
            except LLMError as e:
                degraded = f"Embedding model unavailable ({e.message}); using keyword search only."
        kw_rank: list[str] = []
        fq = fts_query(query)
        if fq:
            ph = ",".join("?" * len(cids))
            kw = self.db.query(
                f"SELECT c.id FROM chunks_fts f JOIN chunks c ON c.rowid=f.rowid "
                f"WHERE chunks_fts MATCH ? AND c.collection_id IN ({ph}) AND c.status='indexed' "
                f"ORDER BY bm25(chunks_fts) LIMIT ?", (fq, *cids, k * 3))
            kw_rank = [r["id"] for r in kw]
        # relevance floor: a vector-only hit below the similarity floor is noise, not evidence
        kw_set = set(kw_rank)
        vec_rank = [i for i in vec_rank if i in kw_set or sims.get(i, 0.0) >= self.min_similarity]
        fused = rrf([vec_rank, kw_rank])
        if not fused:
            return {"hits": [], "degraded": degraded}
        ids = list(fused)
        ph = ",".join("?" * len(ids))
        rows_c = {r["id"]: r for r in self.db.query(
            f"SELECT c.*, d.title AS doc_title, d.source AS source, d.source_type, d.retrieved_at, col.name AS collection "
            f"FROM chunks c JOIN documents d ON d.id=c.document_id JOIN collections col ON col.id=c.collection_id "
            f"WHERE c.id IN ({ph}) AND c.status='indexed'", ids)}
        hits = []
        for id_, sc in fused.items():
            r = rows_c.get(id_)
            if not r:
                continue
            adj = sc * (1.0 + max(-0.5, min(0.5, 0.1 * (r["utility"] or 0))))
            hits.append({"chunk_id": id_, "document_id": r["document_id"], "collection": r["collection"],
                         "text": r["text"], "title": r["doc_title"], "source": r["source"], "locator": r["locator"],
                         "source_type": r["source_type"], "retrieved_at": r["retrieved_at"],
                         "score": round(adj, 5), "similarity": round(sims.get(id_, 0.0), 4), "meta": r["meta"]})
        hits.sort(key=lambda h: -h["score"])
        return {"hits": hits[:k], "degraded": degraded}

    def record_utility(self, chunk_ids: list[str], delta: float) -> None:
        if not chunk_ids:
            return
        with self.db.conn() as c:
            c.executemany("UPDATE chunks SET utility=MAX(-5, MIN(5, COALESCE(utility,0)+?)) WHERE id=?",
                          [(delta, i) for i in chunk_ids])

    # ---- maintenance -----------------------------------------------------------------------
    def reindex(self, cid: str, batch: int = 128, progress: Callable[[int, int], None] | None = None) -> dict:
        """Re-embed every indexed chunk of a collection with the CURRENT embedder."""
        emb = self.get_embedder()
        total = int(self.db.scalar("SELECT COUNT(*) FROM chunks WHERE collection_id=? AND status='indexed'", (cid,), 0))
        self.vectors.drop(cid)
        done = 0
        dim = 0
        last = ""
        while True:
            rows = self.db.query("SELECT id, text FROM chunks WHERE collection_id=? AND status='indexed' AND id>? "
                                 "ORDER BY id LIMIT ?", (cid, last, batch))
            if not rows:
                break
            vecs = emb.embed([r["text"] for r in rows])
            dim = int(vecs.shape[1]) if vecs.size else dim
            self.vectors.upsert(cid, [r["id"] for r in rows], vecs)
            done += len(rows)
            last = rows[-1]["id"]
            if progress:
                progress(done, total)
        self.bind_embedder(cid, dim)
        self.db.audit("system", "collection.reindex", {"id": cid, "chunks": done, "embedder": emb.id})
        return {"collection": cid, "chunks": done, "embedder": emb.id, "dim": dim}

    def approve_quarantined(self, chunk_id: str) -> bool:
        r = self.db.get("chunks", chunk_id)
        if not r or r["status"] != "quarantined":
            return False
        emb = self.get_embedder()
        v = emb.embed([r["text"]])
        self.vectors.upsert(r["collection_id"], [chunk_id], v)
        with self.db.tx() as c:
            c.execute("UPDATE chunks SET status='indexed', reason=reason||' [approved by user]' WHERE id=?", (chunk_id,))
        self.bind_embedder(r["collection_id"], int(v.shape[1]))
        self.db.audit("user", "chunk.approve_quarantined", {"id": chunk_id})
        return True

    def export_collection(self, cid: str) -> list[dict]:
        return self.db.query("SELECT c.id, c.idx, c.text, c.locator, c.status, d.source, d.title FROM chunks c "
                             "JOIN documents d ON d.id=c.document_id WHERE c.collection_id=? ORDER BY d.id, c.idx", (cid,))
