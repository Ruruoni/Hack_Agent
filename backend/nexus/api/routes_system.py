from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from pathlib import Path

from fastapi import Body, FastAPI, Query

from ..config import save_overrides
from ..database.db import now
from ..llm.base import LLMError
from . import setup_wizard


def register(app: FastAPI, ctx) -> None:
    s = ctx.settings

    # ---------------------------------------------------------------- health / dashboard
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": app.version, "stopped": ctx.stop_event.is_set(), "execution_mode": s.execution_mode,
                "vector_backend": ctx.vectors.name, "embedder": ctx.embedder.id, "data_dir": s.data_dir}

    @app.get("/api/dashboard")
    def dashboard():
        h = ctx.provider.health()
        model = caps = None
        if h.get("ok"):
            try:
                caps = ctx.provider.capabilities().to_dict()  # type: ignore[attr-defined]
                model = caps["model"]
            except LLMError as e:
                caps = {"error": e.to_dict()}
        db = ctx.db
        errors = db.query("SELECT ts, kind, message, task_id FROM task_events WHERE level IN ('error','warn') ORDER BY id DESC LIMIT 8")
        failed_jobs = db.query("SELECT id, name, status, error, pause_reason, updated FROM ingestion_jobs WHERE status IN ('failed','paused','completed_with_errors') ORDER BY updated DESC LIMIT 5")
        return {
            "agent": {"status": "stopped" if ctx.stop_event.is_set() else "ready", "execution_mode": s.execution_mode,
                      "pending_approvals": len(ctx.perms.pending()), "version": app.version},
            "llm": {"health": h, "active_model": model or s.chat_model or None, "capabilities": caps},
            "embedding": {"id": ctx.embedder.id, "provider": s.embedding_provider, "model": s.embedding_model or None,
                          "semantic": s.embedding_provider != "hash"},
            "resources": ctx.hardware(),
            "tasks": {"active": ctx.tasks.list("running", 10) + ctx.tasks.list("awaiting_approval", 10) + ctx.tasks.list("planning", 10),
                      "recent": ctx.tasks.list(None, 6)},
            "ingestion": {"running": db.query("SELECT id,name,status FROM ingestion_jobs WHERE status='running'"), "attention": failed_jobs},
            "collections": ctx.kb.list_collections(),
            "learning": ctx.learning.overview(),
            "recent_learning": db.query("SELECT id, layer, kind, trust, title, created FROM memories ORDER BY created DESC LIMIT 6"),
            "recent_errors": errors,
        }

    @app.post("/api/stop")
    def stop():
        ctx.global_stop()
        ctx.ingestion.stop_event.set()
        for t in ctx.tasks.list("running", 50) + ctx.tasks.list("awaiting_approval", 50) + ctx.tasks.list("planning", 50):
            ctx.tasks.cancel(t["id"])
        return {"stopped": True}

    @app.post("/api/resume")
    def resume():
        ctx.clear_stop()
        return {"stopped": False}

    # ---------------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings():
        return s.public_dict()

    @app.put("/api/settings")
    def put_settings(changes: dict = Body(...)):
        applied = save_overrides(s, changes)
        ctx.db.audit("user", "settings.change", {"keys": applied, "values": {k: ("***" if "key" in k else changes[k]) for k in applied}})
        return {"applied": applied, "settings": s.public_dict()}

    @app.get("/api/audit")
    def audit(limit: int = 100, event: str | None = None):
        if event:
            return ctx.db.query("SELECT * FROM audit_log WHERE event LIKE ? ORDER BY id DESC LIMIT ?", (event + "%", limit))
        return ctx.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/audit/verify")
    def audit_verify():
        return ctx.db.verify_audit()

    @app.get("/api/hardware")
    def hardware():
        return ctx.hardware()

    # ---------------------------------------------------------------- LLM / models
    @app.get("/api/llm/status")
    def llm_status():
        h = ctx.provider.health()
        out = {"health": h, "base_url": s.lmstudio_base_url, "configured_model": s.chat_model or None, "context_window": s.context_window, "tool_mode": s.tool_mode}
        if h.get("ok"):
            try:
                out["capabilities"] = ctx.provider.capabilities().to_dict()  # type: ignore[attr-defined]
            except LLMError as e:
                out["capabilities_error"] = e.to_dict()
        return out

    @app.get("/api/llm/models")
    def llm_models():
        return {"models": [m.to_dict() for m in ctx.provider.list_models()], "selected": s.chat_model or None, "embedding_selected": s.embedding_model or None}

    @app.post("/api/llm/select")
    def llm_select(body: dict = Body(...)):
        changes = {k: body[k] for k in ("chat_model", "embedding_model", "embedding_provider", "context_window", "temperature", "top_p", "max_tokens", "tool_mode") if k in body}
        applied = save_overrides(s, changes)
        ctx.db.audit("user", "llm.select", changes)
        return {"applied": applied}

    @app.post("/api/llm/load")
    def llm_load(body: dict = Body(...)):
        return ctx.provider.load_model(body["model"], body.get("context_length"))

    @app.post("/api/llm/unload")
    def llm_unload(body: dict = Body(...)):
        return ctx.provider.unload_model(body["model"])

    @app.post("/api/llm/test")
    def llm_test(body: dict = Body(default={})):
        t0 = time.time()
        r = ctx.provider.chat([{"role": "user", "content": body.get("prompt", "Reply with the single word: ready")}], max_tokens=40, temperature=0)
        return {"reply": r.text, "model": r.model, "seconds": round(time.time() - t0, 2), "usage": r.usage}

    @app.post("/api/llm/probe-tools")
    def llm_probe():
        p = ctx.provider
        return {"native_tool_calling": p.probe_native_tools() if hasattr(p, "probe_native_tools") else False}

    @app.get("/api/embeddings/status")
    def emb_status():
        e = ctx.embedder
        out = {"id": e.id, "provider": s.embedding_provider, "model": s.embedding_model or None, "semantic": s.embedding_provider != "hash",
               "note": None if s.embedding_provider != "hash" else "The built-in hash embedder is lexical (shared words). Select a real embedding model in LM Studio for semantic search."}
        try:
            v = e.embed(["connection test"])
            out.update(ok=True, dim=int(v.shape[1]))
        except LLMError as err:
            out.update(ok=False, error=err.to_dict())
        out["collections_needing_reindex"] = [c["name"] for c in ctx.kb.list_collections() if c.get("needs_reindex")]
        return out

    # ---------------------------------------------------------------- backup
    @app.post("/api/backup")
    def backup():
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = s.path("backups", f"nexus-backup-{ts}.zip")
        tmp = s.path("backups", f"_db_{ts}.sqlite")
        ctx.db.backup_to(tmp)
        mem = s.path("backups", f"_memories_{ts}.json")
        mem.write_text(json.dumps(ctx.memory.export(), default=str, indent=1), encoding="utf-8")
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp, "nexus.db")
            z.write(mem, "memories.json")
            if (Path(s.data_dir) / "settings.json").exists():
                z.write(Path(s.data_dir) / "settings.json", "settings.json")
            for sub in ("reports", "extensions/active"):
                for f in s.path(*sub.split("/")).rglob("*"):
                    if f.is_file():
                        z.write(f, f"{sub}/{f.relative_to(s.path(*sub.split('/')))}")
        tmp.unlink(missing_ok=True)
        mem.unlink(missing_ok=True)
        ctx.db.audit("user", "backup.create", {"file": dest.name})
        return {"file": str(dest), "bytes": dest.stat().st_size,
                "restore": f"Stop NEXUS, then run: python -m nexus.cli restore \"{dest}\"  (vectors are rebuilt with Re-index; raw data folders are not included)."}

    @app.get("/api/backups")
    def backups():
        return [{"name": f.name, "bytes": f.stat().st_size, "modified": f.stat().st_mtime} for f in sorted(s.path("backups").glob("nexus-backup-*.zip"), reverse=True)]

    @app.get("/api/trash")
    def trash():
        out = []
        for m in s.path("trash").glob("*/manifest.json"):
            d = json.loads(m.read_text())
            out.append({"id": m.parent.name, **d})
        return out

    @app.post("/api/trash/{tid}/restore")
    def trash_restore(tid: str):
        folder = s.path("trash", tid)
        man = json.loads((folder / "manifest.json").read_text())
        orig = Path(man["original"])
        item = next(p for p in folder.iterdir() if p.name != "manifest.json")
        if orig.exists():
            raise ValueError(f"{orig} already exists; move it first")
        orig.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item), str(orig))
        shutil.rmtree(folder, ignore_errors=True)
        ctx.db.audit("user", "trash.restore", {"original": str(orig)})
        return {"restored": str(orig)}

    # ---------------------------------------------------------------- setup wizard
    @app.get("/api/setup/steps")
    def setup_steps():
        return setup_wizard.list_steps(ctx)

    @app.post("/api/setup/run/{step}")
    def setup_run(step: str, body: dict = Body(default={})):
        return setup_wizard.run_step(ctx, step, body)

    # ---------------------------------------------------------------- projects
    @app.get("/api/projects")
    def projects():
        return ctx.db.query("SELECT * FROM projects ORDER BY created")

    @app.post("/api/projects")
    def create_project(body: dict = Body(...)):
        from ..database.db import new_id
        pid = new_id("proj_")
        ctx.db.insert("projects", {"id": pid, "name": body["name"], "description": body.get("description", ""), "created": now()})
        return ctx.db.get("projects", pid)
