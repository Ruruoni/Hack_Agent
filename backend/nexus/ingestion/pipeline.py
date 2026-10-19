"""Resumable, batch-oriented ingestion engine.

Memory: files are streamed segment by segment; at most `ingest_batch_size` chunks are held at a time.
Durability: each flush writes (chunks + FTS rows + facts + rejects + item checkpoint) in ONE SQLite transaction,
after the vectors have been upserted (idempotent, deterministic ids). A crash can therefore never leave the
checkpoint ahead of the data; resuming re-reads the file and skips segments below the stored cursor.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import Settings
from ..database.db import Database, dumps, new_id, now
from ..knowledge.store import KnowledgeBase
from ..knowledge.text import (bands, canon, chunk_text, extract_facts, normalize, sha, simhash64, to_signed)
from ..llm.base import LLMError
from ..llm.embeddings import Embedder
from ..system.hardware import check_disk
from . import parsers
from .parsers import ParseError, Profile
from .quality import QualityRules, assess

log = logging.getLogger("nexus.ingest")

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}
FINAL = {"completed", "completed_with_errors", "failed", "cancelled"}
RESUMABLE = {"paused", "interrupted", "queued", "failed", "completed_with_errors", "cancelled"}


class JobInterrupt(Exception):
    def __init__(self, state: str, reason: str = ""):
        super().__init__(reason)
        self.state, self.reason = state, reason


@dataclass
class JobConfig:
    chunk_chars: int = 1200
    chunk_overlap: int = 150
    batch_size: int = 64
    workers: int = 2
    min_chunk_chars: int = 40
    max_reject_rate: float = 0.6
    min_sample_for_gate: int = 50
    near_dup_hamming: int = 3
    quarantine_injections: bool = True
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    include_globs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    recursive: bool = True
    keep_raw: bool = True
    extract_facts: bool = True
    max_file_mb: int = 512
    max_item_seconds: int = 3600
    item_retries: int = 2
    tabular_retrieval: bool = True        # False = profile only (no row chunks indexed)


def default_config(s: Settings) -> JobConfig:
    return JobConfig(chunk_chars=s.chunk_chars, chunk_overlap=s.chunk_overlap, batch_size=s.ingest_batch_size,
                     workers=s.ingest_workers, min_chunk_chars=s.min_chunk_chars, max_reject_rate=s.max_reject_rate,
                     min_sample_for_gate=s.min_sample_for_gate, near_dup_hamming=s.near_dup_hamming,
                     quarantine_injections=s.quarantine_injections, keep_raw=s.keep_raw, max_file_mb=s.max_file_mb,
                     max_item_seconds=s.max_item_seconds)


class _Ctl:
    def __init__(self) -> None:
        self.pause = threading.Event()
        self.cancel = threading.Event()
        self.ignore_gate = False
        self.lock = threading.Lock()
        self.seen = 0       # chunks judged by quality rules (accepted + rejected + quarantined)
        self.bad = 0


class IngestionManager:
    def __init__(self, settings: Settings, db: Database, kb: KnowledgeBase, get_embedder: Callable[[], Embedder],
                 stop_event: threading.Event | None = None, url_fetcher: Callable[[str], dict] | None = None,
                 on_event: Callable[[str, dict], None] | None = None):
        self.s, self.db, self.kb, self.get_embedder = settings, db, kb, get_embedder
        self.stop_event = stop_event or threading.Event()
        self.url_fetcher = url_fetcher
        self.on_event = on_event or (lambda k, d: None)
        self._ctl: dict[str, _Ctl] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ job lifecycle
    def create_job(self, name: str, collection: str, sources: list[str], config: dict | None = None,
                   project_id: str = "default") -> dict:
        cfg = default_config(self.s)
        for k, v in (config or {}).items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        col = self.kb.get_collection(collection) or self.kb.create_collection(collection, project_id=project_id)
        jid = new_id("job_")
        self.db.insert("ingestion_jobs", {"id": jid, "name": name or f"ingest-{time.strftime('%Y%m%d-%H%M%S')}",
                                          "collection_id": col["id"], "status": "queued",
                                          "config": asdict(cfg) | {"sources": sources}, "counts": {},
                                          "created": now(), "updated": now()})
        self.db.audit("user", "ingest.create", {"job": jid, "sources": sources[:20]})
        self._discover(jid, sources, cfg)
        return self.get_job(jid)  # type: ignore[return-value]

    def get_job(self, jid: str) -> dict | None:
        j = self.db.get("ingestion_jobs", jid)
        if not j:
            return None
        j["counts"] = self._aggregate(jid)
        j["live"] = jid in self._threads and self._threads[jid].is_alive()
        return j

    def list_jobs(self, limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT id FROM ingestion_jobs ORDER BY created DESC LIMIT ?", (limit,))
        return [self.get_job(r["id"]) for r in rows]  # type: ignore[misc]

    def start(self, jid: str, force_quality: bool = False) -> None:
        with self._lock:
            t = self._threads.get(jid)
            if t and t.is_alive():
                return
            t = threading.Thread(target=self._safe_run, args=(jid, force_quality), daemon=True, name=f"ingest-{jid}")
            self._threads[jid] = t
            t.start()

    def wait(self, jid: str, timeout: float | None = None) -> None:
        t = self._threads.get(jid)
        if t:
            t.join(timeout)

    def pause(self, jid: str) -> None:
        self._ctl.setdefault(jid, _Ctl()).pause.set()

    def cancel(self, jid: str) -> None:
        self._ctl.setdefault(jid, _Ctl()).cancel.set()
        j = self.db.get("ingestion_jobs", jid)
        if j and not (jid in self._threads and self._threads[jid].is_alive()):
            self.db.update("ingestion_jobs", jid, {"status": "cancelled", "updated": now()})

    def resume(self, jid: str, force_quality: bool = False) -> None:
        j = self.db.get("ingestion_jobs", jid)
        if not j or j["status"] not in RESUMABLE:
            raise ValueError(f"job cannot be resumed from status '{j['status'] if j else 'missing'}'")
        self.start(jid, force_quality)

    def update_config(self, jid: str, changes: dict) -> dict:
        j = self.db.get("ingestion_jobs", jid)
        if not j:
            raise ValueError("no such job")
        cfg = j["config"]
        for k, v in changes.items():
            if k in asdict(JobConfig()):
                cfg[k] = v
        self.db.update("ingestion_jobs", jid, {"config": cfg, "updated": now()})
        self.db.audit("user", "ingest.config", {"job": jid, "changes": changes})
        return self.get_job(jid)  # type: ignore[return-value]

    def retry_failed(self, jid: str, include_rejected: bool = False) -> int:
        sts = ("failed", "rejected") if include_rejected else ("failed",)
        ph = ",".join("?" * len(sts))
        n = self.db.execute(f"UPDATE ingestion_items SET status='pending', error='', attempts=0, updated=? "
                            f"WHERE job_id=? AND status IN ({ph})", (now(), jid, *sts))
        return n

    def recover_interrupted(self) -> list[str]:
        """Called at startup: jobs left 'running' by a crash/restart become 'interrupted' (resumable)."""
        rows = self.db.query("SELECT id FROM ingestion_jobs WHERE status='running'")
        for r in rows:
            self.db.update("ingestion_jobs", r["id"], {"status": "interrupted", "updated": now(),
                                                       "pause_reason": "Application stopped while running; resume to continue from checkpoints."})
        self.db.execute("UPDATE ingestion_items SET status='pending' WHERE status='running'")
        return [r["id"] for r in rows]

    # ------------------------------------------------------------------ discovery
    def _discover(self, jid: str, sources: list[str], cfg: JobConfig) -> None:
        work = self.s.path("workspace", jid)
        found: list[tuple[Path, str, str]] = []   # (real path, display source, origin)
        for src in sources:
            if src.startswith(("http://", "https://")):
                if not self.url_fetcher:
                    self._add_item(jid, src, "", "html", 0, "", "rejected", "URL import not available")
                    continue
                try:
                    r = self.url_fetcher(src)
                    found.append((Path(r["path"]), r.get("final_url", src), "url"))
                except Exception as e:  # noqa: BLE001
                    self._add_item(jid, src, "", "html", 0, "", "failed", f"fetch failed: {e}")
                continue
            p = Path(src).expanduser()
            if not p.exists():
                self._add_item(jid, str(p), str(p), "unsupported", 0, "", "failed", "path does not exist")
            elif p.is_file():
                found.append((p, str(p), ""))
            else:
                walker = os.walk(p) if cfg.recursive else [(str(p), [], [f.name for f in p.iterdir() if f.is_file()])]
                for root, dirs, files in walker:
                    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
                    for fn in sorted(files):
                        fp = Path(root) / fn
                        rel = str(fp.relative_to(p))
                        if cfg.include_globs and not any(fnmatch(rel, g) or fnmatch(fn, g) for g in cfg.include_globs):
                            continue
                        if any(fnmatch(rel, g) or fnmatch(fn, g) for g in cfg.exclude_globs):
                            continue
                        found.append((fp, str(fp), ""))
        queue = list(found)
        depth = 0
        while queue:
            path, disp, origin = queue.pop(0)
            kind = parsers.detect_kind(path)
            if kind == "archive":
                if depth > 50:
                    continue
                try:
                    members = parsers.safe_extract(path, work / (hashlib.sha1(disp.encode()).hexdigest()[:8]),
                                                   self.s.archive_max_members, self.s.archive_max_ratio,
                                                   self.s.archive_max_total_mb * 2**20)
                    for m in members:
                        k2 = parsers.detect_kind(m)
                        if k2 == "archive":
                            queue.append((m, f"{disp}!/{m.name}", "archive"))
                        else:
                            self._add_path_item(jid, m, f"{disp}!/{m.name}", "archive", cfg)
                except ParseError as e:
                    self._add_item(jid, disp, str(path), "archive", path.stat().st_size, "", "rejected", str(e))
                depth += 1
                continue
            self._add_path_item(jid, path, disp, origin, cfg)

    def _add_path_item(self, jid: str, path: Path, disp: str, origin: str, cfg: JobConfig) -> None:
        kind = parsers.detect_kind(path)
        ok, why = parsers.validate_file(path, kind, cfg.max_file_mb * 2**20)
        digest = ""
        if ok:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                while b := f.read(1 << 20):
                    h.update(b)
            digest = h.hexdigest()
        size = path.stat().st_size if path.exists() else 0
        status = "pending" if ok else "rejected"
        if ok:   # identical file already queued in this job -> never process twice (also avoids concurrent races)
            twin = self.db.one("SELECT source FROM ingestion_items WHERE job_id=? AND sha256=? AND status<>'duplicate' LIMIT 1", (jid, digest))
            if twin:
                status, why = "duplicate", f"identical file already ingested from {twin['source']}"
        self._add_item(jid, disp, str(path), kind, size, digest, status, why, origin)

    def _add_item(self, jid: str, source: str, path: str, kind: str, size: int, digest: str, status: str,
                  reason: str, origin: str = "") -> None:
        transforms = [f"discovered via {origin}"] if origin else []
        self.db.insert("ingestion_items", {"id": new_id("it_"), "job_id": jid, "source": source, "path": path, "kind": kind,
                                           "size": size, "sha256": digest, "status": status, "reason": reason,
                                           "cursor": 0, "attempts": 0, "counts": {}, "transforms": transforms,
                                           "created": now(), "updated": now()})

    # ------------------------------------------------------------------ run
    def _safe_run(self, jid: str, force_quality: bool) -> None:
        try:
            self.run(jid, force_quality)
        except Exception as e:  # noqa: BLE001
            log.exception("ingestion job crashed")
            self.db.update("ingestion_jobs", jid, {"status": "failed", "error": f"{e.__class__.__name__}: {e}", "updated": now()})

    def run(self, jid: str, force_quality: bool = False) -> dict:
        j = self.db.get("ingestion_jobs", jid)
        if not j:
            raise ValueError("no such job")
        cfg = JobConfig(**{k: v for k, v in j["config"].items() if k in asdict(JobConfig())})
        ctl = self._ctl[jid] = _Ctl()
        ctl.ignore_gate = force_quality
        # seed the quality gate with prior progress when resuming
        prior = self._aggregate(jid)
        ctl.seen = prior.get("chunks_indexed", 0) + prior.get("chunks_rejected", 0) + prior.get("chunks_quarantined", 0)
        ctl.bad = prior.get("chunks_rejected", 0) + prior.get("chunks_quarantined", 0)

        total = int(self.db.scalar("SELECT COALESCE(SUM(size),0) FROM ingestion_items WHERE job_id=? AND status='pending'", (jid,), 0))
        ok, free = check_disk(self.s.data_dir, total, self.s.min_free_disk_mb)
        if not ok:
            self.db.update("ingestion_jobs", jid, {"status": "failed", "updated": now(),
                           "error": f"Not enough free disk space: {free // 2**20} MB free, need ~{total // 2**20} MB plus {self.s.min_free_disk_mb} MB reserve."})
            return self.get_job(jid)  # type: ignore[return-value]
        self.db.update("ingestion_jobs", jid, {"status": "running", "pause_reason": "", "error": "", "updated": now(),
                                               "started": j.get("started") or now()})
        self.on_event("ingest.started", {"job": jid})
        final = "completed"
        reason = ""
        with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
            while True:
                rows = self.db.query("SELECT id FROM ingestion_items WHERE job_id=? AND status='pending' ORDER BY created, id LIMIT ?",
                                     (jid, max(1, cfg.workers) * 4))
                if not rows:
                    break
                try:
                    for _ in pool.map(lambda r: self._process_item(jid, r["id"], j["collection_id"], cfg, ctl), rows):
                        pass
                except JobInterrupt as ji:
                    final, reason = ji.state, ji.reason
                    break
                if self._should_stop(ctl):
                    final = "cancelled" if (ctl.cancel.is_set() or self.stop_event.is_set()) else "paused"
                    break
        if final == "completed":
            agg = self._aggregate(jid)
            final = "completed_with_errors" if agg.get("items_failed", 0) else "completed"
            self._finalize_report(jid)
        self.db.update("ingestion_jobs", jid, {"status": final, "pause_reason": reason, "finished": now() if final in FINAL else None,
                                               "updated": now()})
        self.on_event("ingest.finished", {"job": jid, "status": final})
        return self.get_job(jid)  # type: ignore[return-value]

    def _should_stop(self, ctl: _Ctl) -> bool:
        return ctl.pause.is_set() or ctl.cancel.is_set() or self.stop_event.is_set()

    def _check(self, ctl: _Ctl) -> None:
        if ctl.cancel.is_set() or self.stop_event.is_set():
            raise JobInterrupt("cancelled", "Cancelled by user / global stop")
        if ctl.pause.is_set():
            raise JobInterrupt("paused", "Paused by user")

    # ------------------------------------------------------------------ item processing
    def _process_item(self, jid: str, item_id: str, collection_id: str, cfg: JobConfig, ctl: _Ctl) -> None:
        item = self.db.get("ingestion_items", item_id)
        if not item or item["status"] != "pending":
            return
        self._check(ctl)
        self.db.update("ingestion_items", item_id, {"status": "running", "updated": now()})
        t0 = time.time()
        try:
            for attempt in range(cfg.item_retries + 1):
                try:
                    self._run_item(jid, item, collection_id, cfg, ctl, t0)
                    return
                except (OSError, MemoryError) as e:
                    if attempt >= cfg.item_retries:
                        raise
                    log.warning("retrying %s after %s", item["source"], e)
                    time.sleep(0.2 * (attempt + 1))
        except JobInterrupt:
            self.db.update("ingestion_items", item_id, {"status": "pending", "updated": now()})
            raise
        except LLMError as e:       # embedding service trouble is not the file's fault: pause, keep checkpoint
            self.db.update("ingestion_items", item_id, {"status": "pending", "updated": now()})
            raise JobInterrupt("paused", f"Embedding model unavailable: {e.message}") from e
        except ParseError as e:
            self._finish_item(item_id, "rejected", str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("item failed: %s", item["source"])
            self.db.update("ingestion_items", item_id, {"status": "failed", "error": f"{e.__class__.__name__}: {e}",
                                                        "attempts": item["attempts"] + 1, "updated": now()})

    def _finish_item(self, item_id: str, status: str, reason: str = "", counts: dict | None = None) -> None:
        ch = {"status": status, "reason": reason, "updated": now()}
        if counts is not None:
            ch["counts"] = counts
        self.db.update("ingestion_items", item_id, ch)

    def _run_item(self, jid: str, item: dict, collection_id: str, cfg: JobConfig, ctl: _Ctl, t0: float) -> None:
        path = Path(item["path"])
        kind = item["kind"]
        counts: dict[str, Any] = dict(item["counts"] or {})
        transforms: list[str] = list(item["transforms"] or [])
        # whole-file duplicate?
        dup = self.db.one("SELECT id, source FROM documents WHERE collection_id=? AND sha256=? AND status='indexed' AND id<>?",
                          (collection_id, item["sha256"], item["document_id"] or ""))
        if dup:
            self._finish_item(item["id"], "duplicate", f"identical file already ingested from {dup['source']}", counts)
            return
        if cfg.keep_raw and not counts.get("raw_path"):
            counts["raw_path"] = self._preserve_raw(jid, path, item)
            if counts["raw_path"]:
                transforms.append("raw input preserved")
        doc_id = item["document_id"] or "doc_" + hashlib.sha1(f"{collection_id}:{item['id']}".encode()).hexdigest()[:20]
        if not item["document_id"]:
            self.db.insert("documents", {"id": doc_id, "collection_id": collection_id, "source": item["source"],
                                         "source_type": kind, "title": Path(item["source"].split("!/")[-1]).name or item["source"],
                                         "sha256": item["sha256"], "status": "indexing", "retrieved_at": now(),
                                         "raw_path": counts.get("raw_path", ""), "meta": {"job": jid}, "created": now(), "updated": now()})
            self.db.update("ingestion_items", item["id"], {"document_id": doc_id})
        rules = QualityRules(min_chars=cfg.min_chunk_chars, quarantine_injections=cfg.quarantine_injections,
                             include_patterns=cfg.include_patterns, exclude_patterns=cfg.exclude_patterns,
                             structured=kind in parsers.TABULAR)
        inc, exc = rules.compiled()
        cursor = int(item["cursor"] or 0)
        profile = Profile()
        buf_rejects: list[tuple[int, str, str]] = []
        state = {"skipping": cursor > 0}

        def reject(idx: int, reason: str, sample: str) -> None:
            if not state["skipping"]:
                buf_rejects.append((idx, reason, sample[:300]))

        if kind in parsers.TABULAR and not cfg.tabular_retrieval:
            for _ in parsers.iter_segments(path, kind, reject, profile):
                self._check(ctl)
            self._save_profile(jid, item, profile)
            counts["records_seen"] = profile.rows
            counts["records_rejected"] = counts.get("records_rejected", 0) + len(buf_rejects)
            self._write_rejects(jid, item["id"], buf_rejects)
            self.db.update("documents", doc_id, {"status": "indexed", "updated": now()})
            self._finish_item(item["id"], "done", "profile only (row indexing disabled)", counts)
            return

        it = iter(parsers.iter_segments(path, kind, reject, profile))
        seg_index = 0
        pending: list[dict] = []
        chunk_idx = int(counts.get("chunk_idx", 0))
        local_hashes: set[str] = set()
        local_sims: list[int] = []
        transforms += [f"normalized (NFC, control chars removed, whitespace collapsed)",
                       f"chunked at {cfg.chunk_chars} chars / {cfg.chunk_overlap} overlap"]
        while True:
            state["skipping"] = seg_index + 1 <= cursor
            seg = next(it, None)
            if seg is None:
                break
            seg_index += 1
            if seg_index <= cursor:
                continue
            if time.time() - t0 > cfg.max_item_seconds:
                raise ParseError(f"exceeded max processing time ({cfg.max_item_seconds}s)")
            text = normalize(seg.text)
            for piece in chunk_text(text, cfg.chunk_chars, cfg.chunk_overlap):
                chunk_idx += 1
                pending.append({"text": piece, "locator": seg.locator, "idx": chunk_idx, "meta": seg.meta})
            if len(pending) >= cfg.batch_size:
                self._check(ctl)
                self._flush(jid, item, doc_id, collection_id, cfg, rules, inc, exc, pending, seg_index, chunk_idx,
                            counts, buf_rejects, local_hashes, local_sims, ctl)
                pending, buf_rejects = [], []
        state["skipping"] = False
        if pending or buf_rejects or seg_index != cursor:
            self._flush(jid, item, doc_id, collection_id, cfg, rules, inc, exc, pending, seg_index, chunk_idx, counts,
                        buf_rejects, local_hashes, local_sims, ctl)
        if kind in parsers.TABULAR:
            self._save_profile(jid, item, profile)
            counts["records_seen"] = profile.rows
            if profile.to_dict()["schema_drift"]:
                counts["schema_drift"] = True
        total_indexed = counts.get("chunks_indexed", 0)
        status = "done"
        reason = ""
        if total_indexed == 0 and counts.get("chunks_quarantined", 0) > 0:
            status, reason = "quarantined", "all content was quarantined for review"
        elif total_indexed == 0 and seg_index == 0:
            status, reason = "rejected", "no extractable content"
        elif total_indexed == 0:
            status, reason = "rejected", "all content rejected by quality filters"
        bad = counts.get("chunks_rejected", 0) + counts.get("records_rejected", 0) + counts.get("chunks_quarantined", 0)
        good = total_indexed
        if good + bad > 5 and bad / (good + bad) > 0.5:
            counts["poor_quality"] = True
        self.db.update("documents", doc_id, {"status": "indexed" if total_indexed else "empty", "updated": now(),
                                              "meta": {"job": jid, "locators": seg_index, "poor_quality": bool(counts.get("poor_quality"))}})
        self.db.update("ingestion_items", item["id"], {"transforms": transforms})
        self._finish_item(item["id"], status, reason, counts)

    def _preserve_raw(self, jid: str, path: Path, item: dict) -> str:
        data_root = Path(self.s.data_dir).resolve()
        try:
            rp = path.resolve()
            if data_root in rp.parents and rp.parts[len(data_root.parts)] in ("raw", "downloads", "uploads"):
                return str(rp)     # already stored under a preserved area
            dest = self.s.path("raw", jid, f"{item['sha256'][:12]}_{path.name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                try:
                    os.link(rp, dest)
                except OSError:
                    shutil.copy2(rp, dest)
            return str(dest)
        except OSError as e:
            log.warning("could not preserve raw input %s: %s", path, e)
            return ""

    def _save_profile(self, jid: str, item: dict, profile: Profile) -> None:
        self.db.execute("DELETE FROM dataset_profiles WHERE item_id=?", (item["id"],))
        self.db.insert("dataset_profiles", {"id": new_id("pf_"), "item_id": item["id"], "job_id": jid, "source": item["source"],
                                            "profile": profile.to_dict(), "created": now()})

    def _write_rejects(self, jid: str, item_id: str, rejects: Iterable[tuple[int, str, str]]) -> None:
        rows = [(jid, item_id, i, r, s, now()) for i, r, s in rejects]
        if rows:
            with self.db.conn() as c:
                c.executemany("INSERT INTO ingestion_rejects(job_id,item_id,record_index,reason,sample,created) VALUES(?,?,?,?,?,?)", rows)

    def _flush(self, jid, item, doc_id, coll_id, cfg: JobConfig, rules: QualityRules, inc, exc, pending: list[dict],
               cursor_after: int, chunk_idx: int, counts: dict, rejects: list, local_hashes: set, local_sims: list,
               ctl: _Ctl) -> None:
        st = Counter()
        accepted: list[dict] = []
        quarantined: list[dict] = []
        chunk_rejects: list[tuple[int, str, str]] = []
        with self.db.conn() as c:
            for ch in pending:
                text = ch["text"]
                verdict, why = assess(text, rules, inc, exc)
                if verdict == "reject":
                    st["chunks_rejected"] += 1
                    chunk_rejects.append((ch["idx"], why, text[:200]))
                    continue
                key = sha(canon(text))
                sim = simhash64(text)
                ch.update(sha=key, sim=sim, reason=why)
                if key in local_hashes or self.kb.find_exact(c, coll_id, key):
                    st["chunks_duplicate"] += 1
                    continue
                if any(bin(sim ^ s).count("1") <= cfg.near_dup_hamming for s in local_sims) or \
                        self.kb.find_near(c, coll_id, sim, cfg.near_dup_hamming):
                    st["chunks_near_duplicate"] += 1
                    continue
                local_hashes.add(key)
                if len(local_sims) < 2000:
                    local_sims.append(sim)
                (quarantined if verdict == "quarantine" else accepted).append(ch)
        st["chunks_quarantined"] += len(quarantined)
        # 1) vectors first (idempotent upsert); 2) one atomic SQL transaction that also advances the checkpoint
        emb = self.get_embedder()
        vecs = emb.embed([c["text"] for c in accepted]) if accepted else None
        for ch in accepted + quarantined:
            ch["id"] = "ch_" + hashlib.sha1(f"{doc_id}:{ch['sha']}".encode()).hexdigest()[:24]
        if accepted:
            self.kb.vectors.upsert(coll_id, [c["id"] for c in accepted], vecs)
        facts_rows = []
        if cfg.extract_facts and counts.get("facts", 0) < 200:
            for ch in accepted[:50]:
                for s_, p_, o_ in extract_facts(ch["text"])[:4]:
                    facts_rows.append((new_id("f_"), coll_id, doc_id, ch["id"], s_, p_, o_, 0.3, "extracted", now()))
        with self.db.tx() as c:
            # authoritative duplicate check: writers are serialized by BEGIN IMMEDIATE, so concurrent workers
            # that raced past the cheap pre-check above cannot both insert the same content.
            dropped: list[str] = []
            keep_a, keep_q = [], []
            for group, keep in ((accepted, keep_a), (quarantined, keep_q)):
                for ch in group:
                    if self.kb.find_exact(c, coll_id, ch["sha"]):
                        st["chunks_duplicate"] += 1
                    elif self.kb.find_near(c, coll_id, ch["sim"], cfg.near_dup_hamming):
                        st["chunks_near_duplicate"] += 1
                    else:
                        keep.append(ch)
                        continue
                    dropped.append(ch["id"])
            accepted, quarantined = keep_a, keep_q
            st["chunks_quarantined"] = len(quarantined)
            rows = []
            for ch, status in [(x, "indexed") for x in accepted] + [(x, "quarantined") for x in quarantined]:
                b = bands(ch["sim"])
                rows.append((ch["id"], doc_id, coll_id, ch["idx"], ch["text"], ch["sha"], to_signed(ch["sim"]), *map(int, b),
                             status, ch.get("reason", ""), ch["locator"], dumps(ch["meta"]), now()))
            c.executemany("INSERT OR IGNORE INTO chunks(id,document_id,collection_id,idx,text,sha,simhash,b0,b1,b2,b3,status,reason,"
                          "locator,meta,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            ids = [a["id"] for a in accepted]
            c.executemany("INSERT INTO chunks_fts(rowid,text) SELECT rowid,text FROM chunks WHERE id=? "
                          "AND NOT EXISTS (SELECT 1 FROM chunks_fts WHERE rowid=chunks.rowid)", [(i,) for i in ids])
            if facts_rows:
                c.executemany("INSERT INTO facts(id,collection_id,document_id,chunk_id,subject,predicate,object,confidence,trust,created) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?)", facts_rows)
            all_rej = [(jid, item["id"], i, r, s, now()) for i, r, s in rejects] + \
                      [(jid, item["id"], i, r, s, now()) for i, r, s in chunk_rejects]
            if all_rej:
                c.executemany("INSERT INTO ingestion_rejects(job_id,item_id,record_index,reason,sample,created) VALUES(?,?,?,?,?,?)", all_rej)
            st["records_rejected"] = len(rejects)
            st["chunks_indexed"] = len(accepted)
            for k, v in st.items():
                counts[k] = counts.get(k, 0) + v
            counts["chunk_idx"] = chunk_idx
            counts["facts"] = counts.get("facts", 0) + len(facts_rows)
            c.execute("UPDATE ingestion_items SET cursor=?, counts=?, updated=? WHERE id=?",
                      (cursor_after, dumps(counts), now(), item["id"]))
        if dropped:
            self.kb.vectors.delete(coll_id, dropped)       # orphan vectors from the rare raced duplicates
        if accepted and vecs is not None:
            self.kb.bind_embedder(coll_id, int(vecs.shape[1]))
        # quality gate
        with ctl.lock:
            judged = st["chunks_indexed"] + st["chunks_rejected"] + st["chunks_quarantined"]
            ctl.seen += judged
            ctl.bad += st["chunks_rejected"] + st["chunks_quarantined"]
            tripped = (not ctl.ignore_gate and ctl.seen >= cfg.min_sample_for_gate
                       and ctl.bad / ctl.seen > cfg.max_reject_rate)
            seen, bad = ctl.seen, ctl.bad
        self.on_event("ingest.progress", {"job": jid, "item": item["id"], "indexed": counts.get("chunks_indexed", 0)})
        if tripped:
            raise JobInterrupt("paused", f"Quality gate: {bad}/{seen} chunks ({bad / seen:.0%}) rejected or quarantined, "
                                         f"above the {cfg.max_reject_rate:.0%} threshold. Inspect the rejects, adjust filters, then resume (or force).")
        self._check(ctl)

    # ------------------------------------------------------------------ reporting
    def _aggregate(self, jid: str) -> dict:
        agg: Counter = Counter()
        status: Counter = Counter()
        for r in self.db.query("SELECT status, counts FROM ingestion_items WHERE job_id=?", (jid,)):
            status[r["status"]] += 1
            for k, v in (r["counts"] or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in ("chunk_idx",):
                    agg[k] += v
        total = sum(status.values())
        out = {k: 0 for k in ("chunks_indexed", "chunks_duplicate", "chunks_near_duplicate", "chunks_rejected",
                              "chunks_quarantined", "records_seen", "records_rejected")}
        out.update(agg)
        out.update({"items_discovered": total, "items_done": status["done"], "items_duplicate": status["duplicate"],
                    "items_rejected": status["rejected"], "items_quarantined": status["quarantined"],
                    "items_failed": status["failed"], "items_pending": status["pending"] + status["running"]})
        out["processed"] = status["done"] + status["duplicate"] + status["rejected"] + status["quarantined"] + status["failed"]
        out["duplicated"] = out.get("chunks_duplicate", 0) + out.get("chunks_near_duplicate", 0) + status["duplicate"]
        out["rejected"] = out.get("chunks_rejected", 0) + out.get("records_rejected", 0) + status["rejected"]
        out["quarantined"] = out.get("chunks_quarantined", 0)
        out["failed"] = status["failed"]
        return out

    def list_items(self, jid: str, status: str | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
        if status:
            return self.db.query("SELECT * FROM ingestion_items WHERE job_id=? AND status=? ORDER BY created LIMIT ? OFFSET ?",
                                 (jid, status, limit, offset))
        return self.db.query("SELECT * FROM ingestion_items WHERE job_id=? ORDER BY created LIMIT ? OFFSET ?", (jid, limit, offset))

    def list_rejects(self, jid: str, limit: int = 200, offset: int = 0) -> list[dict]:
        return self.db.query("SELECT * FROM ingestion_rejects WHERE job_id=? ORDER BY id LIMIT ? OFFSET ?", (jid, limit, offset))

    def quarantined_chunks(self, jid: str, limit: int = 100) -> list[dict]:
        return self.db.query("SELECT c.id, c.text, c.reason, c.locator, d.source FROM chunks c JOIN documents d ON d.id=c.document_id "
                             "WHERE c.status='quarantined' AND c.document_id IN (SELECT document_id FROM ingestion_items WHERE job_id=?) LIMIT ?",
                             (jid, limit))

    def _retrieval_check(self, jid: str, collection_id: str, n: int = 20) -> dict:
        rows = self.db.query("SELECT id, text FROM chunks WHERE status='indexed' AND document_id IN "
                             "(SELECT document_id FROM ingestion_items WHERE job_id=? AND document_id IS NOT NULL) "
                             "ORDER BY RANDOM() LIMIT ?", (jid, n))
        hits = 0
        for r in rows:
            q = " ".join(r["text"].split()[:30])
            res = self.kb.search(q, [collection_id], k=5)
            if any(h["chunk_id"] == r["id"] for h in res["hits"]):
                hits += 1
        return {"sampled": len(rows), "found_in_top5": hits, "recall_at_5": round(hits / len(rows), 3) if rows else None}

    def _finalize_report(self, jid: str) -> dict:
        rep = self.report(jid, with_retrieval_check=True)
        self.db.update("ingestion_jobs", jid, {"report": rep["markdown"]})
        d = self.s.path("reports")
        (d / f"ingest_{jid}.json").write_text(json.dumps(rep["data"], indent=2, default=str), encoding="utf-8")
        (d / f"ingest_{jid}.md").write_text(rep["markdown"], encoding="utf-8")
        return rep

    def report(self, jid: str, with_retrieval_check: bool = False) -> dict:
        j = self.db.get("ingestion_jobs", jid)
        if not j:
            raise ValueError("no such job")
        agg = self._aggregate(jid)
        reasons = Counter()
        for r in self.db.query("SELECT reason FROM ingestion_rejects WHERE job_id=?", (jid,)):
            reasons[(r["reason"] or "").split(":")[0][:60]] += 1
        items = self.db.query("SELECT source, kind, status, reason, counts, error FROM ingestion_items WHERE job_id=?", (jid,))
        poor = [i["source"] for i in items if (i["counts"] or {}).get("poor_quality")]
        drift = [i["source"] for i in items if (i["counts"] or {}).get("schema_drift")]
        profiles = self.db.query("SELECT source, profile FROM dataset_profiles WHERE job_id=?", (jid,))
        conflicts = self.db.query(
            "SELECT subject, predicate, COUNT(DISTINCT object) AS variants FROM facts WHERE collection_id=? "
            "AND predicate NOT LIKE 'mentions_%' GROUP BY subject, predicate HAVING COUNT(DISTINCT object)>1 LIMIT 20",
            (j["collection_id"],))
        rc = self._retrieval_check(jid, j["collection_id"]) if with_retrieval_check else None
        data = {"job": jid, "name": j["name"], "status": j["status"], "counts": agg, "reject_reasons": dict(reasons),
                "poor_quality_sources": poor, "schema_drift_sources": drift, "possible_conflicting_facts": conflicts,
                "datasets": [{"source": p["source"], "rows": p["profile"]["rows"], "columns": list(p["profile"]["columns"]),
                              "malformed_rows": p["profile"]["malformed_rows"]} for p in profiles],
                "retrieval_check": rc, "failed_items": [{"source": i["source"], "error": i["error"]} for i in items if i["status"] == "failed"],
                "rejected_items": [{"source": i["source"], "reason": i["reason"]} for i in items if i["status"] == "rejected"]}
        md = [f"# Ingestion report - {j['name']}", f"*Job `{jid}` - status **{j['status']}***", "",
              "## Counts", "", "| Metric | Value |", "|---|---|"]
        for k, label in [("items_discovered", "Items discovered"), ("processed", "Items processed"), ("items_done", "Items indexed OK"),
                         ("chunks_indexed", "Chunks indexed"), ("duplicated", "Duplicates (chunk + file)"),
                         ("rejected", "Rejected (records, chunks, files)"), ("quarantined", "Chunks quarantined"),
                         ("failed", "Items failed")]:
            md.append(f"| {label} | {agg.get(k, 0)} |")
        if reasons:
            md += ["", "## Why records were rejected", ""] + [f"- {r}: {n}" for r, n in reasons.most_common(10)]
        if data["failed_items"]:
            md += ["", "## Failed items (retryable)", ""] + [f"- `{i['source']}` - {i['error']}" for i in data["failed_items"][:20]]
        if data["rejected_items"]:
            md += ["", "## Rejected files", ""] + [f"- `{i['source']}` - {i['reason']}" for i in data["rejected_items"][:20]]
        if poor:
            md += ["", "## Poor-quality sources (>50% bad content)", ""] + [f"- `{p}`" for p in poor[:20]]
        if drift:
            md += ["", "## Unexpected schema changes inside file", ""] + [f"- `{p}`" for p in drift[:20]]
        if conflicts:
            md += ["", "## Possibly conflicting extracted facts (review manually)", ""] + \
                  [f"- {c['subject']} / {c['predicate']}: {c['variants']} different values" for c in conflicts]
        if rc:
            md += ["", "## Retrieval self-check", "", f"Sampled {rc['sampled']} chunks; {rc['found_in_top5']} found in top-5 "
                   f"(recall@5 = {rc['recall_at_5']}). This checks the index works; it does not prove the content is true."]
        md += ["", "> Indexing adds searchable knowledge. It does **not** change the language model's weights."]
        return {"data": data, "markdown": "\n".join(md)}
