from __future__ import annotations

import csv
import io
import json
import os
import string
from pathlib import Path

from fastapi import Body, FastAPI, Query
from fastapi.responses import FileResponse, Response

from ..database.db import now
from ..system.hardware import estimate_ingest


def register(app: FastAPI, ctx) -> None:
    s = ctx.settings
    db = ctx.db

    # ---------------------------------------------------------------- local file browser (for the UI folder picker)
    @app.get("/api/fs/browse")
    def browse(path: str = ""):
        if not path:
            roots = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")] if os.name == "nt" else ["/"]
            return {"path": "", "parent": None, "entries": [{"name": r, "dir": True, "path": r, "size": 0} for r in roots], "home": str(Path.home())}
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            raise ValueError("not a directory")
        entries = []
        for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:500]:
            try:
                entries.append({"name": c.name, "dir": c.is_dir(), "path": str(c), "size": c.stat().st_size if c.is_file() else 0})
            except OSError:
                continue
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "entries": entries, "home": str(Path.home())}

    # ---------------------------------------------------------------- collections & documents
    @app.get("/api/collections")
    def collections(project_id: str | None = None):
        return ctx.kb.list_collections(project_id)

    @app.post("/api/collections")
    def create_collection(body: dict = Body(...)):
        return ctx.kb.create_collection(body["name"], body.get("description", ""), body.get("project_id", "default"))

    @app.delete("/api/collections/{cid}")
    def delete_collection(cid: str):
        ctx.kb.delete_collection(cid)
        return {"deleted": cid}

    @app.post("/api/collections/{cid}/reindex")
    def reindex(cid: str):
        return ctx.kb.reindex(cid)

    @app.get("/api/collections/{cid}/export")
    def export_collection(cid: str, fmt: str = "json"):
        rows = ctx.kb.export_collection(cid)
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(rows[0]) if rows else ["id"])
            w.writeheader()
            w.writerows(rows)
            return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=collection_{cid}.csv"})
        return Response(json.dumps(rows, indent=1), media_type="application/json", headers={"Content-Disposition": f"attachment; filename=collection_{cid}.json"})

    @app.get("/api/collections/{cid}/documents")
    def documents(cid: str, limit: int = 200, offset: int = 0):
        return db.query("SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.document_id=d.id AND c.status='indexed') AS chunks FROM documents d "
                        "WHERE collection_id=? ORDER BY created DESC LIMIT ? OFFSET ?", (cid, limit, offset))

    @app.get("/api/documents/{did}")
    def document(did: str):
        d = db.get("documents", did)
        if not d:
            raise ValueError("no such document")
        return {**d, "chunks": db.query("SELECT id, idx, locator, status, reason, substr(text,1,400) AS text FROM chunks WHERE document_id=? ORDER BY idx LIMIT 200", (did,)),
                "facts": db.query("SELECT subject,predicate,object,confidence,trust FROM facts WHERE document_id=? LIMIT 50", (did,))}

    @app.delete("/api/documents/{did}")
    def delete_document(did: str):
        ctx.kb.delete_document(did)
        db.audit("user", "document.delete", {"id": did})
        return {"deleted": did}

    @app.post("/api/knowledge/search")
    def knowledge_search(body: dict = Body(...)):
        return ctx.kb.search(body["query"], body.get("collections") or None, int(body.get("k", 8)))

    @app.get("/api/quarantine")
    def quarantine(job: str | None = None, limit: int = 100):
        if job:
            return ctx.ingestion.quarantined_chunks(job, limit)
        return db.query("SELECT c.id, c.text, c.reason, c.locator, d.source FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.status='quarantined' LIMIT ?", (limit,))

    @app.post("/api/quarantine/{chunk_id}/approve")
    def quarantine_approve(chunk_id: str):
        return {"approved": ctx.kb.approve_quarantined(chunk_id)}

    @app.delete("/api/quarantine/{chunk_id}")
    def quarantine_discard(chunk_id: str):
        db.execute("DELETE FROM chunks_fts WHERE rowid=(SELECT rowid FROM chunks WHERE id=?)", (chunk_id,))
        db.execute("DELETE FROM chunks WHERE id=? AND status='quarantined'", (chunk_id,))
        db.audit("user", "chunk.discard_quarantined", {"id": chunk_id})
        return {"discarded": chunk_id}

    # ---------------------------------------------------------------- memory
    @app.get("/api/memories")
    def memories(layer: str | None = None, q: str | None = None, status: str | None = "active", project_id: str | None = None, limit: int = 200):
        if q:
            return ctx.memory.search(q, layers=[layer] if layer else None, project_id=project_id, k=min(limit, 50), include_outdated=status != "active")
        return ctx.memory.list(layer, project_id, status if status != "all" else None, limit)

    @app.get("/api/memories/counts")
    def memory_counts():
        return ctx.memory.counts()

    @app.post("/api/memories")
    def add_memory(body: dict = Body(...)):
        return ctx.memory.add(body.get("layer", "semantic"), body["content"], kind=body.get("kind", "fact"), title=body.get("title", ""),
                              project_id=body.get("project_id", "default"), trust="user_verified", confidence=body.get("confidence", 0.9), source_type="user")

    @app.get("/api/memories/export")
    def export_memories():
        return Response(json.dumps(ctx.memory.export(), indent=1, default=str), media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=nexus_memories.json"})

    @app.post("/api/memories/reindex")
    def reindex_memories():
        return {"reindexed": ctx.memory.reindex()}

    @app.get("/api/memories/{mid}")
    def memory(mid: str):
        m = ctx.memory.get(mid)
        if not m:
            raise ValueError("no such memory")
        src = None
        if m["source_type"] == "user_feedback" and m["source_ref"]:
            src = db.get("messages", m["source_ref"])
        elif m["source_type"] == "task" and m["source_ref"]:
            src = db.get("tasks", m["source_ref"])
        return {**m, "source": src, "supersedes_memory": ctx.memory.get(m["supersedes"]) if m["supersedes"] else None}

    @app.patch("/api/memories/{mid}")
    def edit_memory(mid: str, body: dict = Body(...)):
        db.audit("user", "memory.edit", {"id": mid, "fields": list(body)})
        return ctx.memory.update(mid, **body)

    @app.post("/api/memories/{mid}/outdated")
    def outdated(mid: str):
        ctx.memory.mark_outdated(mid)
        return ctx.memory.get(mid)

    @app.post("/api/memories/{mid}/verify")
    def verify_memory(mid: str):
        db.audit("user", "memory.verify", {"id": mid})
        return ctx.memory.update(mid, trust="user_verified", confidence=0.95)

    @app.delete("/api/memories/{mid}")
    def delete_memory(mid: str):
        ctx.memory.delete(mid)
        return {"deleted": mid}

    # ---------------------------------------------------------------- ingestion
    @app.post("/api/ingest/estimate")
    def ingest_estimate(body: dict = Body(...)):
        total, n = 0, 0
        for src in body["sources"]:
            p = Path(src).expanduser()
            if p.is_file():
                total, n = total + p.stat().st_size, n + 1
            elif p.is_dir():
                for r, _, fs in os.walk(p):
                    for f in fs:
                        try:
                            total += (Path(r) / f).stat().st_size
                            n += 1
                        except OSError:
                            pass
                    if n > 200000:
                        break
        hw = ctx.hardware()
        return {"files": n, "bytes": total, **estimate_ingest(total), "disk_free_mb": hw["disk_free_mb"], "embedder": ctx.embedder.id,
                "suggested_batch_size": 16 if hw["ram_available_mb"] < 3000 else 64 if hw["ram_available_mb"] < 12000 else 128,
                "warning": "Disk space is low" if hw["disk_free_mb"] * 2**20 < total * 2 + s.min_free_disk_mb * 2**20 else None}

    @app.post("/api/ingest/jobs")
    def ingest_create(body: dict = Body(...)):
        job = ctx.ingestion.create_job(body.get("name", ""), body.get("collection", "default"), body["sources"], body.get("config"), body.get("project_id", "default"))
        if body.get("start", True):
            ctx.ingestion.start(job["id"])
        return ctx.ingestion.get_job(job["id"])

    @app.get("/api/ingest/jobs")
    def ingest_list():
        return ctx.ingestion.list_jobs()

    @app.get("/api/ingest/jobs/{jid}")
    def ingest_get(jid: str):
        j = ctx.ingestion.get_job(jid)
        if not j:
            raise ValueError("no such job")
        return j

    @app.post("/api/ingest/jobs/{jid}/{action}")
    def ingest_action(jid: str, action: str, body: dict = Body(default={})):
        m = ctx.ingestion
        if action == "start":
            m.start(jid, bool(body.get("force_quality")))
        elif action == "resume":
            m.resume(jid, bool(body.get("force_quality")))
        elif action == "pause":
            m.pause(jid)
        elif action == "cancel":
            m.cancel(jid)
        elif action == "retry":
            n = m.retry_failed(jid, bool(body.get("include_rejected")))
            if n:
                m.resume(jid)
            return {"retried": n}
        else:
            raise ValueError("unknown action")
        return m.get_job(jid)

    @app.patch("/api/ingest/jobs/{jid}/config")
    def ingest_config(jid: str, body: dict = Body(...)):
        return ctx.ingestion.update_config(jid, body)

    @app.get("/api/ingest/jobs/{jid}/items")
    def ingest_items(jid: str, status: str | None = None, limit: int = 200, offset: int = 0):
        return ctx.ingestion.list_items(jid, status, limit, offset)

    @app.get("/api/ingest/jobs/{jid}/rejects")
    def ingest_rejects(jid: str, limit: int = 200, offset: int = 0):
        return ctx.ingestion.list_rejects(jid, limit, offset)

    @app.get("/api/ingest/jobs/{jid}/report")
    def ingest_report(jid: str):
        return ctx.ingestion.report(jid, with_retrieval_check=True)

    @app.get("/api/ingest/jobs/{jid}/profiles")
    def ingest_profiles(jid: str):
        return db.query("SELECT source, profile FROM dataset_profiles WHERE job_id=?", (jid,))

    # ---------------------------------------------------------------- datasets
    @app.post("/api/datasets/inspect")
    def ds_inspect(body: dict = Body(...)):
        return ctx.datasets.inspect(body["ref"])

    @app.post("/api/datasets/plan")
    def ds_plan(body: dict = Body(...)):
        return ctx.datasets.plan(body["ref"], body.get("files"), body.get("splits"))

    @app.post("/api/datasets/download")
    def ds_download(body: dict = Body(...)):
        return ctx.datasets.start_download(body["ref"], body.get("files"), body.get("splits"), bool(body.get("approve_large")))

    @app.get("/api/datasets/downloads")
    def ds_list():
        return ctx.datasets.list()

    @app.get("/api/datasets/downloads/{did}")
    def ds_get(did: str):
        d = ctx.datasets.get(did)
        if not d:
            raise ValueError("no such download")
        return {**d, "problems": ctx.datasets.verify(did) if d["status"] in ("ready", "failed") else []}

    @app.post("/api/datasets/downloads/{did}/cancel")
    def ds_cancel(did: str):
        ctx.datasets.cancel(did)
        return {"cancelling": True}

    @app.get("/api/datasets/downloads/{did}/preview")
    def ds_preview(did: str, file: str | None = None, n: int = 5):
        files = ctx.datasets.files_of(did)
        f = next((x for x in files if x.name == file or str(x).endswith(file or "\0")), files[0] if files else None)
        if not f:
            raise ValueError("no files")
        return {"files": [x.name for x in files], **ctx.datasets.preview(f, n)}

    @app.post("/api/datasets/downloads/{did}/import")
    def ds_import(did: str, body: dict = Body(...)):
        return ctx.datasets.import_dataset(did, body.get("collection", "datasets"), body.get("config"))

    # ---------------------------------------------------------------- web research
    @app.post("/api/research/jobs")
    def rs_create(body: dict = Body(...)):
        job = ctx.research.create_job(body.get("query", ""), body.get("urls"), body.get("params"))
        if body.get("start", True):
            ctx.research.start(job["id"])
        return ctx.research.get_job(job["id"])

    @app.get("/api/research/jobs")
    def rs_list():
        return ctx.research.list_jobs()

    @app.get("/api/research/jobs/{jid}")
    def rs_get(jid: str):
        j = ctx.research.get_job(jid)
        if not j:
            raise ValueError("no such job")
        return j

    @app.post("/api/research/jobs/{jid}/start")
    def rs_start(jid: str):
        ctx.research.start(jid)
        return ctx.research.get_job(jid)

    @app.post("/api/research/jobs/{jid}/cancel")
    def rs_cancel(jid: str):
        ctx.research.cancel(jid)
        return {"cancelling": True}

    @app.get("/api/research/jobs/{jid}/pages")
    def rs_pages(jid: str, status: str | None = None):
        return ctx.research.pages(jid, status)

    @app.get("/api/research/pages/{pid}")
    def rs_page(pid: str):
        return db.get("research_pages", pid)

    @app.post("/api/research/approve")
    def rs_approve(body: dict = Body(...)):
        return {"updated": ctx.research.approve(body["page_ids"], body.get("approved", True))}

    @app.get("/api/research/jobs/{jid}/export")
    def rs_export(jid: str, fmt: str = "json"):
        p = ctx.research.export(jid, fmt)
        return FileResponse(p, filename=p.name)

    @app.post("/api/research/jobs/{jid}/add-to-knowledge")
    def rs_add(jid: str, body: dict = Body(...)):
        return ctx.research.add_to_knowledge(ctx.ingestion, jid, body.get("collection", "research"), body.get("page_ids"))

    @app.post("/api/web/import-url")
    def import_url(body: dict = Body(...)):
        job = ctx.ingestion.create_job(f"url import", body.get("collection", "web"), [body["url"]])
        ctx.ingestion.run(job["id"])
        return ctx.ingestion.get_job(job["id"])
