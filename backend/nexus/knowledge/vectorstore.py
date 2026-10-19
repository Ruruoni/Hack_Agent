"""Vector storage behind one interface. ChromaDB when available, otherwise a SQLite brute-force store."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from ..database.db import Database

log = logging.getLogger("nexus.vectors")


class VectorStore(ABC):
    name = "base"

    @abstractmethod
    def upsert(self, collection: str, ids: list[str], vectors: np.ndarray, metas: list[dict] | None = None) -> None: ...

    @abstractmethod
    def query(self, collection: str, vector: np.ndarray, k: int = 5) -> list[tuple[str, float]]:
        """Returns [(id, similarity)] best first (cosine similarity, higher is better)."""

    @abstractmethod
    def delete(self, collection: str, ids: list[str]) -> None: ...

    @abstractmethod
    def drop(self, collection: str) -> None: ...

    @abstractmethod
    def count(self, collection: str) -> int: ...


class SqliteVectorStore(VectorStore):
    """float32 blobs in SQLite, searched in bounded numpy blocks (never loads the whole index at once)."""
    name = "sqlite"
    BLOCK = 20000

    def __init__(self, db: Database):
        self.db = db

    def upsert(self, collection, ids, vectors, metas=None):
        rows = [(i, collection, np.asarray(v, dtype=np.float32).tobytes()) for i, v in zip(ids, vectors)]
        with self.db.conn() as c:
            c.executemany("INSERT OR REPLACE INTO vectors(id,collection,vec) VALUES(?,?,?)", rows)

    def query(self, collection, vector, k=5):
        q = np.asarray(vector, dtype=np.float32)
        best: list[tuple[str, float]] = []
        with self.db.conn() as c:
            cur = c.execute("SELECT id, vec FROM vectors WHERE collection=?", (collection,))
            while True:
                rows = cur.fetchmany(self.BLOCK)
                if not rows:
                    break
                mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
                if mat.shape[1] != q.shape[0]:
                    return []
                sims = mat @ q
                top = np.argsort(-sims)[:k]
                best += [(rows[i][0], float(sims[i])) for i in top]
                best = sorted(best, key=lambda x: -x[1])[:k]
        return best

    def delete(self, collection, ids):
        with self.db.conn() as c:
            c.executemany("DELETE FROM vectors WHERE id=? AND collection=?", [(i, collection) for i in ids])

    def drop(self, collection):
        self.db.execute("DELETE FROM vectors WHERE collection=?", (collection,))

    def count(self, collection):
        return int(self.db.scalar("SELECT COUNT(*) FROM vectors WHERE collection=?", (collection,), 0))


class ChromaVectorStore(VectorStore):
    name = "chroma"

    def __init__(self, path: Path):
        import chromadb  # type: ignore
        from chromadb.config import Settings as CS  # type: ignore
        path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(path), settings=CS(anonymized_telemetry=False))

    def _coll(self, name: str):
        return self.client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})

    def upsert(self, collection, ids, vectors, metas=None):
        if not ids:
            return
        c = self._coll(collection)
        arr = np.asarray(vectors, dtype=np.float32)
        for i in range(0, len(ids), 2000):
            kw = {"metadatas": metas[i:i + 2000]} if metas and any(metas) else {}
            c.upsert(ids=ids[i:i + 2000], embeddings=arr[i:i + 2000].tolist(), **kw)

    def query(self, collection, vector, k=5):
        c = self._coll(collection)
        n = c.count()
        if n == 0:
            return []
        try:
            res = c.query(query_embeddings=[np.asarray(vector, dtype=np.float32).tolist()], n_results=min(k, n))
        except Exception as e:  # noqa: BLE001 - dimension mismatch etc.
            log.warning("chroma query failed: %s", e)
            return []
        return [(i, 1.0 - float(d)) for i, d in zip(res["ids"][0], res["distances"][0])]

    def delete(self, collection, ids):
        if ids:
            self._coll(collection).delete(ids=ids)

    def drop(self, collection):
        try:
            self.client.delete_collection(collection)
        except Exception:  # noqa: BLE001 - already gone
            pass

    def count(self, collection):
        return self._coll(collection).count()


def build_vector_store(settings, db: Database) -> VectorStore:
    want = settings.vector_backend
    if want in ("auto", "chroma"):
        try:
            return ChromaVectorStore(settings.path("vectors"))
        except Exception as e:  # noqa: BLE001
            if want == "chroma":
                raise
            log.warning("ChromaDB unavailable (%s); using SQLite vector store", e)
    return SqliteVectorStore(db)
