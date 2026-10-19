from __future__ import annotations

import re
import threading
import time
from typing import Iterator

from fastapi import Body, FastAPI, File, Form, UploadFile
from fastapi.responses import StreamingResponse

from ..agent.chat import ChatService
from ..database.db import new_id
from ..tasks.manager import TERMINAL


def register(app: FastAPI, ctx) -> None:
    chat = ChatService(ctx)
    ctx.chat = chat

    def sse(obj: dict) -> str:
        import json
        return "data: " + json.dumps(obj, default=str) + "\n\n"

    # ---------------------------------------------------------------- conversations
    @app.get("/api/conversations")
    def conversations(project_id: str | None = None):
        return chat.list(project_id)

    @app.post("/api/conversations")
    def create_conversation(body: dict = Body(default={})):
        return chat.create(body.get("title"), body.get("project_id", "default"))

    @app.get("/api/conversations/{cid}")
    def conversation(cid: str):
        c = chat.get(cid)
        if not c:
            raise ValueError("no such conversation")
        return {**c, "messages": chat.messages(cid)}

    @app.patch("/api/conversations/{cid}")
    def rename_conversation(cid: str, body: dict = Body(...)):
        ctx.db.update("conversations", cid, {k: body[k] for k in ("title", "project_id") if k in body})
        return chat.get(cid)

    @app.delete("/api/conversations/{cid}")
    def delete_conversation(cid: str):
        chat.delete(cid)
        return {"deleted": cid}

    @app.post("/api/conversations/{cid}/summarize")
    def summarize(cid: str):
        return {"summary": chat.summarize(cid)}

    # ---------------------------------------------------------------- chat (SSE)
    def agent_stream(cid: str, text: str) -> Iterator[dict]:
        """Agent mode: create a task bound to this conversation and relay its progress as chat events."""
        user_msg = chat.add_message(cid, "user", text, {"mode": "agent"})
        yield {"type": "meta", "conversation_id": cid, "user_message_id": user_msg["id"]}
        conv = chat.get(cid)
        task = ctx.tasks.create(text, conversation_id=cid, project_id=conv["project_id"] if conv else "default")
        yield {"type": "task", "task": {"id": task["id"], "status": task["status"]}}
        last_ev, last_status = 0, ""
        while True:
            for e in ctx.tasks.events(task["id"], last_ev):
                last_ev = e["id"]
                yield {"type": "task_event", "event": {"kind": e["kind"], "level": e["level"], "message": e["message"], "data": e["data"]}}
            t = ctx.tasks.get(task["id"])
            if t["status"] != last_status:
                last_status = t["status"]
                yield {"type": "task", "task": {"id": t["id"], "status": t["status"], "progress": t["progress"]}}
            if t["status"] in TERMINAL or t["status"] == "awaiting_input":
                break
            time.sleep(0.3)
        for e in ctx.tasks.events(task["id"], last_ev):
            yield {"type": "task_event", "event": {"kind": e["kind"], "level": e["level"], "message": e["message"], "data": e["data"]}}
        t = ctx.tasks.get(task["id"])
        body = t["result"] or t["error"] or "(no result)"
        if t["status"] == "failed" and t["error"] and t["error"] not in body:
            body = f"**The task did not finish:** {t['error']}\n\n{body}"
        msg = chat.add_message(cid, "assistant", body, {"task_id": t["id"], "task_status": t["status"], "mode": "agent"})
        yield {"type": "done", "message": msg}

    @app.post("/api/chat")
    def chat_endpoint(body: dict = Body(...)):
        cid = body.get("conversation_id")
        if not cid:
            cid = chat.create(project_id=body.get("project_id", "default"))["id"]
        text = body["message"]

        def gen():
            try:
                stream = agent_stream(cid, text) if body.get("mode") == "agent" else chat.send(
                    cid, text, collections=body.get("collections") or None, use_retrieval=body.get("use_retrieval", True),
                    overrides=body.get("overrides"), attachments=body.get("attachments"))
                for ev in stream:
                    yield sse(ev)
            except Exception as e:  # noqa: BLE001 - a stream must end with an event, not a broken connection
                yield sse({"type": "error", "error": {"code": "internal", "message": f"{e.__class__.__name__}: {e}"}})
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/chat/{cid}/stop")
    def chat_stop(cid: str):
        chat.stop(cid)
        return {"stopping": True}

    # ---------------------------------------------------------------- feedback
    @app.post("/api/feedback")
    def feedback(body: dict = Body(...)):
        return ctx.learning.submit_feedback(body["conversation_id"], body.get("message_id"), body.get("rating"), body.get("correction", ""),
                                            body.get("scope"), body.get("replaces_memory_id"))

    # ---------------------------------------------------------------- uploads -> ingestion
    @app.post("/api/uploads")
    async def upload(files: list[UploadFile] = File(...), collection: str = Form("uploads"), wait: str = Form("true"),   # (FastAPI turns an absent bool Form field into False, so parse a string)
                     
                     conversation_id: str | None = Form(None)):
        folder = ctx.settings.path("uploads", time.strftime("%Y%m%d-%H%M%S") + "-" + new_id()[:6])
        folder.mkdir(parents=True, exist_ok=True)
        saved = []
        limit = ctx.settings.max_file_mb * 2**20
        for f in files:
            name = re.sub(r"[^A-Za-z0-9._\- ]", "_", (f.filename or "upload").replace("\\", "/").split("/")[-1])[:150] or "upload"
            dest = folder / name
            n = 0
            with open(dest, "wb") as out:
                while chunk := await f.read(1 << 20):
                    n += len(chunk)
                    if n > limit:
                        out.close()
                        dest.unlink(missing_ok=True)
                        raise ValueError(f"'{name}' exceeds the {ctx.settings.max_file_mb} MB upload limit; use the folder/dataset import for large data")
                    out.write(chunk)
            saved.append(str(dest))
        job = ctx.ingestion.create_job(f"upload ({len(saved)} file(s))", collection, saved)
        ctx.ingestion.start(job["id"])
        if wait.lower() in ("1", "true", "yes", "on"):
            ctx.ingestion.wait(job["id"], 120)
        j = ctx.ingestion.get_job(job["id"])
        docs = ctx.db.query("SELECT id, title, source FROM documents WHERE id IN (SELECT document_id FROM ingestion_items WHERE job_id=? AND document_id IS NOT NULL)", (job["id"],))
        if conversation_id and docs:
            chat.set_state(conversation_id, last_documents=[{"title": d["title"], "document_id": d["id"]} for d in docs])
        return {"job": j, "documents": docs}
