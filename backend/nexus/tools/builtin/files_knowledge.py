"""File, knowledge, memory and ingestion tools."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from ...database.db import new_id, now
from ...permissions.policy import DELETE, INGEST, MEM_WRITE, READ, SCRATCH, WRITE, PermissionDenied
from ..registry import Tool, ToolContext, ToolRegistry, fail, ok, truncate


def _perm(ctx: ToolContext):
    return ctx.app.perms.paths


# ---------------------------------------------------------------- filesystem
def fs_list(ctx: ToolContext, a: dict) -> dict:
    p = _perm(ctx).resolve(a.get("path") or ".")
    if not p.is_dir():
        return fail(f"not a directory: {p}")
    pat = a.get("pattern") or "*"
    items = []
    for c in sorted(p.glob(pat))[: int(a.get("max_items", 200))]:
        try:
            st = c.stat()
        except OSError:
            continue
        items.append({"name": c.name, "type": "dir" if c.is_dir() else "file", "size": st.st_size, "modified": st.st_mtime})
    return ok(path=str(p), items=items, count=len(items))


def fs_read(ctx: ToolContext, a: dict) -> dict:
    p = _perm(ctx).resolve(a["path"])
    if not p.is_file():
        return fail(f"not a file: {p}")
    off, lim = int(a.get("offset", 0)), min(int(a.get("max_chars", 8000)), 60000)
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        f.seek(off)
        text = f.read(lim + 1)
    return ok(path=str(p), content=text[:lim], truncated=len(text) > lim, next_offset=off + lim if len(text) > lim else None,
              size=p.stat().st_size)


def _write_effects(ctx: ToolContext, a: dict) -> set[str]:
    p = _perm(ctx).resolve(a["path"], write=True)
    return {SCRATCH} if _perm(ctx).is_scratch(p) else {WRITE}


def fs_write(ctx: ToolContext, a: dict) -> dict:
    pp = _perm(ctx)
    p = pp.resolve(a["path"], write=True)
    if p.is_dir():
        return fail("path is a directory")
    p.parent.mkdir(parents=True, exist_ok=True)
    backup = ""
    if p.exists() and a.get("mode", "write") == "write":     # never overwrite silently: keep a recoverable copy
        b = ctx.app.settings.path("backups", f"{int(time.time())}_{p.name}")
        shutil.copy2(p, b)
        backup = str(b)
    with open(p, "a" if a.get("mode") == "append" else "w", encoding="utf-8", newline="") as f:
        f.write(a["content"])
    return ok(path=str(p), bytes=len(a["content"].encode()), backup=backup)


def _verify_write(ctx: ToolContext, a: dict, r: dict) -> tuple[bool, str]:
    p = Path(r["path"])
    if not p.is_file():
        return False, "file not found after write"
    if a.get("mode") == "append":
        return p.read_text(encoding="utf-8", errors="replace").endswith(a["content"]), "re-read file: appended text present"
    got = hashlib.sha256(p.read_bytes()).hexdigest()
    want = hashlib.sha256(a["content"].encode()).hexdigest()
    return got == want, "re-read file and compared SHA-256 with the intended content"


def fs_delete(ctx: ToolContext, a: dict) -> dict:
    """Soft delete: moves to data/trash with a manifest so the human can restore it."""
    p = _perm(ctx).resolve(a["path"], write=True)
    if not p.exists():
        return fail("path does not exist")
    tid = new_id("trash_")
    dest = ctx.app.settings.path("trash", tid)
    dest.mkdir(parents=True)
    shutil.move(str(p), str(dest / p.name))
    (dest / "manifest.json").write_text(json.dumps({"original": str(p), "deleted_at": now(), "task": ctx.task_id}), encoding="utf-8")
    return ok(deleted=str(p), trash_id=tid, recoverable=True)


def _verify_delete(ctx: ToolContext, a: dict, r: dict) -> tuple[bool, str]:
    gone = not Path(r["deleted"]).exists()
    in_trash = (ctx.app.settings.path("trash", r["trash_id"], Path(r["deleted"]).name)).exists()
    return gone and in_trash, "original path is gone and a recoverable copy exists in trash"


# ---------------------------------------------------------------- knowledge & memory
def knowledge_search(ctx: ToolContext, a: dict) -> dict:
    cols = [a["collection"]] if a.get("collection") else None
    res = ctx.app.kb.search(a["query"], cols, k=int(a.get("k", 5)))
    return ok(degraded=res["degraded"], results=[{"title": h["title"], "source": h["source"], "locator": h["locator"],
                                                  "collection": h["collection"], "score": h["score"], "text": h["text"][:1200]}
                                                 for h in res["hits"]])


def memory_search(ctx: ToolContext, a: dict) -> dict:
    layers = [a["layer"]] if a.get("layer") else None
    rows = ctx.app.memory.search(a["query"], layers=layers, k=int(a.get("k", 5)))
    return ok(results=[{"id": m["id"], "layer": m["layer"], "trust": m["trust"], "content": m["content"][:600],
                        "updated": m["updated"]} for m in rows])


def memory_save(ctx: ToolContext, a: dict) -> dict:
    """Agent-authored memories are ALWAYS stored unverified (or hypothesis) - only the user can verify."""
    trust = "hypothesis" if a.get("kind") == "hypothesis" else "unverified"
    m = ctx.app.memory.add(a.get("layer", "semantic"), a["content"], kind=a.get("kind", "fact"), trust=trust, confidence=0.4,
                           source_type="agent", source_ref=ctx.task_id or "")
    return ok(id=m["id"], trust=trust, note="Saved as unverified; a human can verify it in the Memory page.")


def ingest_path(ctx: ToolContext, a: dict) -> dict:
    p = _perm(ctx).resolve(a["path"])
    job = ctx.app.ingestion.create_job(a.get("name") or f"agent-ingest-{p.name}", a.get("collection", "agent"), [str(p)])
    ctx.app.ingestion.run(job["id"])
    j = ctx.app.ingestion.get_job(job["id"])
    return ok(job=job["id"], status=j["status"], counts=j["counts"])


def register(reg: ToolRegistry) -> None:
    S = lambda **p: {"type": "object", "properties": p, "additionalProperties": False}  # noqa: E731
    reg.register(Tool("fs_list", "List files in an allowed folder (default: the agent workspace).",
                      S(path={"type": "string"}, pattern={"type": "string"}, max_items={"type": "integer"}), fs_list))
    reg.register(Tool("fs_read", "Read a text file from an allowed folder.",
                      {**S(path={"type": "string"}, offset={"type": "integer"}, max_chars={"type": "integer"}), "required": ["path"]},
                      fs_read, untrusted_output=True))
    reg.register(Tool("fs_write", "Write or append a text file. Existing files are backed up first.",
                      {**S(path={"type": "string"}, content={"type": "string"}, mode={"type": "string", "enum": ["write", "append"]}),
                       "required": ["path", "content"]},
                      fs_write, effects=_write_effects, risk="medium", verify_fn=_verify_write))
    reg.register(Tool("fs_delete", "Delete a file or folder (moved to a recoverable trash, never destroyed).",
                      {**S(path={"type": "string"}), "required": ["path"]}, fs_delete, effects={DELETE}, risk="high",
                      verify_fn=_verify_delete))
    reg.register(Tool("knowledge_search", "Semantic + keyword search over ingested documents. Returns text with source and locator.",
                      {**S(query={"type": "string"}, collection={"type": "string"}, k={"type": "integer"}), "required": ["query"]},
                      knowledge_search, untrusted_output=True))
    reg.register(Tool("memory_search", "Search persistent memory (facts, preferences, procedures, past task episodes).",
                      {**S(query={"type": "string"}, layer={"type": "string", "enum": ["episodic", "semantic", "procedural", "preference", "project"]},
                           k={"type": "integer"}), "required": ["query"]}, memory_search))
    reg.register(Tool("memory_save", "Save a note to memory as UNVERIFIED (a human must verify before it is trusted).",
                      {**S(content={"type": "string"}, layer={"type": "string", "enum": ["semantic", "episodic", "procedural", "project"]},
                           kind={"type": "string", "enum": ["fact", "hypothesis", "note", "procedure"]}), "required": ["content"]},
                      memory_save, effects={MEM_WRITE}))
    reg.register(Tool("ingest_path", "Ingest a file or folder from an allowed location into a knowledge collection.",
                      {**S(path={"type": "string"}, collection={"type": "string"}, name={"type": "string"}), "required": ["path"]},
                      ingest_path, effects={INGEST, WRITE}, risk="medium", timeout=1800))
