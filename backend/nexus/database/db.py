"""SQLite access layer. One short-lived connection per call keeps threads safe (WAL mode).

Upgrade path to PostgreSQL: all SQL is confined to the repository-style helpers in this package and the
stores that use `Database.query/execute`; the SQL used is plain ANSI apart from FTS5 (search) and
`INSERT OR IGNORE`, which map to tsvector and `ON CONFLICT DO NOTHING` respectively.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', created REAL);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT DEFAULT 'New conversation', summary TEXT DEFAULT '',
  summarized_upto INTEGER DEFAULT 0, project_id TEXT DEFAULT 'default', state TEXT DEFAULT '{}',
  created REAL, updated REAL);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, seq INTEGER, role TEXT NOT NULL,
  content TEXT NOT NULL, meta TEXT DEFAULT '{}', created REAL);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, seq);

CREATE TABLE IF NOT EXISTS collections (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', project_id TEXT DEFAULT 'default',
  embedder TEXT DEFAULT '', dim INTEGER DEFAULT 0, created REAL, updated REAL,
  UNIQUE(name, project_id));

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, collection_id TEXT NOT NULL, source TEXT, source_type TEXT, title TEXT,
  sha256 TEXT, status TEXT DEFAULT 'indexed', retrieved_at REAL, raw_path TEXT, meta TEXT DEFAULT '{}',
  created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS idx_doc_coll ON documents(collection_id);
CREATE INDEX IF NOT EXISTS idx_doc_sha ON documents(collection_id, sha256);

CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL, collection_id TEXT NOT NULL, idx INTEGER,
  text TEXT NOT NULL, sha TEXT, simhash INTEGER, b0 INTEGER, b1 INTEGER, b2 INTEGER, b3 INTEGER,
  status TEXT DEFAULT 'indexed', reason TEXT DEFAULT '', locator TEXT DEFAULT '', meta TEXT DEFAULT '{}',
  utility REAL DEFAULT 0, created REAL);
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunk_coll ON chunks(collection_id, status);
CREATE INDEX IF NOT EXISTS idx_chunk_sha ON chunks(collection_id, sha);
CREATE INDEX IF NOT EXISTS idx_chunk_b0 ON chunks(collection_id, b0);
CREATE INDEX IF NOT EXISTS idx_chunk_b1 ON chunks(collection_id, b1);
CREATE INDEX IF NOT EXISTS idx_chunk_b2 ON chunks(collection_id, b2);
CREATE INDEX IF NOT EXISTS idx_chunk_b3 ON chunks(collection_id, b3);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS vectors (
  id TEXT PRIMARY KEY, collection TEXT NOT NULL, vec BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS idx_vec_coll ON vectors(collection);

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY, collection_id TEXT, document_id TEXT, chunk_id TEXT, subject TEXT, predicate TEXT,
  object TEXT, confidence REAL DEFAULT 0.3, trust TEXT DEFAULT 'extracted', created REAL);
CREATE INDEX IF NOT EXISTS idx_fact_doc ON facts(document_id);
CREATE INDEX IF NOT EXISTS idx_fact_subj ON facts(subject);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, layer TEXT NOT NULL, kind TEXT DEFAULT 'fact', project_id TEXT DEFAULT 'default',
  title TEXT DEFAULT '', content TEXT NOT NULL, confidence REAL DEFAULT 0.5, trust TEXT DEFAULT 'unverified',
  status TEXT DEFAULT 'active', source_type TEXT DEFAULT '', source_ref TEXT DEFAULT '', conversation_id TEXT,
  supersedes TEXT, utility REAL DEFAULT 0, uses INTEGER DEFAULT 0, meta TEXT DEFAULT '{}',
  created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS idx_mem_layer ON memories(layer, status, project_id);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS ingestion_jobs (
  id TEXT PRIMARY KEY, name TEXT, collection_id TEXT, status TEXT, config TEXT DEFAULT '{}',
  counts TEXT DEFAULT '{}', pause_reason TEXT DEFAULT '', error TEXT DEFAULT '', report TEXT DEFAULT '',
  created REAL, updated REAL, started REAL, finished REAL);

CREATE TABLE IF NOT EXISTS ingestion_items (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT, kind TEXT, size INTEGER, sha256 TEXT,
  path TEXT DEFAULT '', status TEXT DEFAULT 'pending', reason TEXT DEFAULT '', cursor INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0, counts TEXT DEFAULT '{}', document_id TEXT, error TEXT DEFAULT '', transforms TEXT DEFAULT '[]',
  created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS idx_item_job ON ingestion_items(job_id, status);

CREATE TABLE IF NOT EXISTS ingestion_rejects (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, item_id TEXT, record_index INTEGER, reason TEXT,
  sample TEXT, created REAL);
CREATE INDEX IF NOT EXISTS idx_rej_job ON ingestion_rejects(job_id);

CREATE TABLE IF NOT EXISTS dataset_profiles (
  id TEXT PRIMARY KEY, item_id TEXT, job_id TEXT, source TEXT, profile TEXT, created REAL);

CREATE TABLE IF NOT EXISTS downloads (
  id TEXT PRIMARY KEY, kind TEXT, ref TEXT, status TEXT, dest TEXT, files TEXT DEFAULT '[]',
  total_bytes INTEGER DEFAULT 0, done_bytes INTEGER DEFAULT 0, error TEXT DEFAULT '', meta TEXT DEFAULT '{}',
  created REAL, updated REAL);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, objective TEXT, status TEXT, conversation_id TEXT, project_id TEXT DEFAULT 'default',
  plan TEXT DEFAULT '{}', state TEXT DEFAULT '{}', progress REAL DEFAULT 0, result TEXT DEFAULT '',
  error TEXT DEFAULT '', created REAL, updated REAL, finished REAL);

CREATE TABLE IF NOT EXISTS task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, ts REAL, level TEXT, kind TEXT, message TEXT,
  data TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_te_task ON task_events(task_id, id);

CREATE TABLE IF NOT EXISTS tool_runs (
  id TEXT PRIMARY KEY, task_id TEXT, tool TEXT, args TEXT, state TEXT, result TEXT DEFAULT '',
  verified INTEGER DEFAULT 0, verification TEXT DEFAULT '', error TEXT DEFAULT '', created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS idx_tr_task ON tool_runs(task_id);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, task_id TEXT, kind TEXT DEFAULT 'tool', tool TEXT, args TEXT, risk TEXT, reason TEXT,
  status TEXT DEFAULT 'pending', requested REAL, decided REAL, decided_by TEXT DEFAULT '', note TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_appr_status ON approvals(status);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, event TEXT, details TEXT, prev_hash TEXT, hash TEXT);

CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER DEFAULT 1, kind TEXT DEFAULT 'workflow',
  status TEXT DEFAULT 'candidate', description TEXT DEFAULT '', spec TEXT DEFAULT '{}', origin TEXT DEFAULT '',
  tests TEXT DEFAULT '{}', successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS idx_skill_name ON skills(name, version);

CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, conversation_id TEXT, message_id TEXT, rating INTEGER, correction TEXT, scope TEXT,
  memory_id TEXT, created REAL);

CREATE TABLE IF NOT EXISTS eval_cases (
  id TEXT PRIMARY KEY, name TEXT, kind TEXT, spec TEXT, origin TEXT DEFAULT '', active INTEGER DEFAULT 1,
  created REAL);
CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY, ts REAL, summary TEXT, results TEXT);

CREATE TABLE IF NOT EXISTS research_jobs (
  id TEXT PRIMARY KEY, query TEXT, params TEXT DEFAULT '{}', status TEXT, frontier TEXT DEFAULT '[]',
  counts TEXT DEFAULT '{}', error TEXT DEFAULT '', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS research_pages (
  id TEXT PRIMARY KEY, job_id TEXT, url TEXT, final_url TEXT, status TEXT, http_status INTEGER, title TEXT,
  text TEXT, meta TEXT DEFAULT '{}', content_hash TEXT, flagged TEXT DEFAULT '', raw_path TEXT,
  retrieved_at REAL, approved INTEGER DEFAULT 0, depth INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_rp_job ON research_pages(job_id);

CREATE TABLE IF NOT EXISTS training_runs (
  id TEXT PRIMARY KEY, base_model TEXT, status TEXT, config TEXT DEFAULT '{}', hardware TEXT DEFAULT '{}',
  dataset TEXT DEFAULT '{}', estimate TEXT DEFAULT '{}', metrics TEXT DEFAULT '{}', log_path TEXT,
  output_dir TEXT, decision TEXT DEFAULT '', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS model_registry (
  id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT, base TEXT, run_id TEXT, status TEXT, meta TEXT DEFAULT '{}',
  created REAL);
"""

JSON_COLS = {"meta", "state", "plan", "config", "counts", "args", "spec", "tests", "profile", "params",
             "frontier", "files", "details", "data", "hardware", "dataset", "estimate", "metrics",
             "results", "summary", "transforms"}


def now() -> float:
    return time.time()


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:16]


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in list(d):
        if k in JSON_COLS and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k]) if d[k] else None
            except ValueError:
                pass
    return d


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._audit_lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    # -- connections ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = self._connect()
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction: everything inside commits atomically (used for ingestion checkpoints)."""
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    # -- helpers ----------------------------------------------------------------------
    def init(self) -> None:
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            c.execute("INSERT OR IGNORE INTO projects(id,name,description,created) VALUES('default','default','Default project',?)", (now(),))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self.conn() as c:
            return [_row(r) for r in c.execute(sql, tuple(params)).fetchall()]  # type: ignore[misc]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self.conn() as c:
            return _row(c.execute(sql, tuple(params)).fetchone())

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.conn() as c:
            return c.execute(sql, tuple(params)).rowcount

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        with self.conn() as c:
            r = c.execute(sql, tuple(params)).fetchone()
            return r[0] if r and r[0] is not None else default

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = list(row)
        vals = [dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()]
        sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        with self.conn() as c:
            c.execute(sql, vals)

    def update(self, table: str, id_: str, changes: dict[str, Any], key: str = "id") -> None:
        if not changes:
            return
        vals = [dumps(v) if isinstance(v, (dict, list)) else v for v in changes.values()]
        sql = f"UPDATE {table} SET {','.join(f'{k}=?' for k in changes)} WHERE {key}=?"
        with self.conn() as c:
            c.execute(sql, [*vals, id_])

    def get(self, table: str, id_: str) -> dict | None:
        return self.one(f"SELECT * FROM {table} WHERE id=?", (id_,))

    # -- audit (hash-chained, append-only by convention; verify() detects tampering) ---------
    def audit(self, actor: str, event: str, details: dict | None = None) -> None:
        with self._audit_lock:
            with self.conn() as c:
                prev = c.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
                prev_hash = prev[0] if prev else ""
                ts = now()
                d = dumps(details or {})
                h = hashlib.sha256(f"{prev_hash}|{ts}|{actor}|{event}|{d}".encode()).hexdigest()
                c.execute("INSERT INTO audit_log(ts,actor,event,details,prev_hash,hash) VALUES(?,?,?,?,?,?)",
                          (ts, actor, event, d, prev_hash, h))

    def verify_audit(self) -> dict:
        prev = ""
        n = 0
        with self.conn() as c:
            for r in c.execute("SELECT * FROM audit_log ORDER BY id"):
                h = hashlib.sha256(f"{prev}|{r['ts']}|{r['actor']}|{r['event']}|{r['details']}".encode()).hexdigest()
                if r["prev_hash"] != prev or r["hash"] != h:
                    return {"ok": False, "broken_at": r["id"], "checked": n}
                prev = r["hash"]
                n += 1
        return {"ok": True, "checked": n}

    def backup_to(self, dest: Path) -> None:
        with self.conn() as src:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
