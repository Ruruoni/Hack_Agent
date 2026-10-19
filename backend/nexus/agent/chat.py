"""Conversational layer: persistent chats, summaries, retrieval-grounded streaming answers."""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Iterator

from ..database.db import dumps, new_id, now
from ..llm.base import LLMError, estimate_tokens
from .prompts import build_system_prompt

_REMEMBER = re.compile(r"^\s*(?:please\s+)?(?:remember|note|keep in mind)(?: that)?[:,]?\s+(.{4,500})$", re.I | re.S)
_PREF = re.compile(r"^\s*(?:from now on|going forward|always|never)[,:]?\s+(.{4,400})$", re.I | re.S)
_REF_WORDS = re.compile(r"\b(it|that|this|those|these|them|the (previous|last|above|same)|continue|again|more|also|"
                        r"the document|that document|the result|the file|former|latter)\b", re.I)
_CITE = re.compile(r"\[S(\d+)\]")


class ChatService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.db = ctx.db
        self._streams: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------ conversations
    def create(self, title: str | None = None, project_id: str = "default") -> dict:
        cid = new_id("conv_")
        self.db.insert("conversations", {"id": cid, "title": title or "New conversation", "summary": "", "project_id": project_id,
                                         "state": {}, "created": now(), "updated": now()})
        return self.db.get("conversations", cid)  # type: ignore[return-value]

    def get(self, cid: str) -> dict | None:
        return self.db.get("conversations", cid)

    def list(self, project_id: str | None = None, limit: int = 100) -> list[dict]:
        if project_id:
            return self.db.query("SELECT * FROM conversations WHERE project_id=? ORDER BY updated DESC LIMIT ?", (project_id, limit))
        return self.db.query("SELECT * FROM conversations ORDER BY updated DESC LIMIT ?", (limit,))

    def delete(self, cid: str) -> None:
        self.db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        self.db.execute("DELETE FROM conversations WHERE id=?", (cid,))

    def messages(self, cid: str, limit: int = 500) -> list[dict]:
        return self.db.query("SELECT * FROM messages WHERE conversation_id=? ORDER BY seq LIMIT ?", (cid, limit))

    def add_message(self, cid: str, role: str, content: str, meta: dict | None = None) -> dict:
        seq = int(self.db.scalar("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE conversation_id=?", (cid,), 1))
        mid = new_id("msg_")
        self.db.insert("messages", {"id": mid, "conversation_id": cid, "seq": seq, "role": role, "content": content,
                                    "meta": meta or {}, "created": now()})
        self.db.execute("UPDATE conversations SET updated=? WHERE id=?", (now(), cid))
        return self.db.get("messages", mid)  # type: ignore[return-value]

    def set_state(self, cid: str, **kv: Any) -> None:
        conv = self.get(cid)
        if not conv:
            return
        st = conv["state"] or {}
        st.update(kv)
        self.db.update("conversations", cid, {"state": st})

    def stop(self, cid: str) -> None:
        ev = self._streams.get(cid)
        if ev:
            ev.set()

    # ------------------------------------------------------------------ prompt assembly
    def _retrieval_query(self, cid: str, text: str) -> str:
        """Resolve 'that document' style follow-ups by borrowing context from the previous turn."""
        if len(text.split()) > 12 and not _REF_WORDS.search(text):
            return text
        prev = self.db.query("SELECT role, content FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT 4", (cid,))
        extra = " ".join(m["content"][:200] for m in reversed(prev))
        return f"{text} {extra}".strip()

    def _history(self, cid: str, budget_tokens: int) -> list[dict]:
        n = self.ctx.settings.history_messages
        rows = self.db.query("SELECT role, content FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT ?", (cid, n))
        out: list[dict] = []
        used = 0
        for r in rows:           # newest first, stop when the budget is exhausted
            t = estimate_tokens(r["content"])
            if used + t > budget_tokens and out:
                break
            used += t
            out.append({"role": r["role"], "content": r["content"]})
        return list(reversed(out))

    def build_messages(self, cid: str, user_text: str, *, collections: list[str] | None, use_retrieval: bool,
                       overrides: dict | None = None) -> tuple[list[dict], list[dict], dict]:
        s = self.ctx.settings
        ov = overrides or {}
        conv = self.get(cid) or {}
        project = conv.get("project_id", "default")
        notes: list[str] = []
        sources: list[dict] = []
        degraded = None
        mem_hits: list[dict] = []
        if use_retrieval:
            q = self._retrieval_query(cid, user_text)
            res = self.ctx.kb.search(q, collections, k=s.retrieval_top_k, project_id=None)
            degraded = res["degraded"]
            for i, h in enumerate(res["hits"], 1):
                sources.append({"n": i, "type": "document", "title": h["title"], "source": h["source"], "locator": h["locator"],
                                "chunk_id": h["chunk_id"], "text": h["text"], "collection": h["collection"],
                                "retrieved_at": h["retrieved_at"], "score": h["score"]})
            mem_hits = self.ctx.memory.search(q, layers=["semantic", "procedural", "episodic", "project"], project_id=project,
                                              k=3, conversation_id=cid)
            for m in mem_hits:
                sources.append({"n": len(sources) + 1, "type": "memory", "title": m["title"], "source": f"memory:{m['id']}",
                                "locator": f"{m['layer']}/{m['trust']}", "chunk_id": m["id"], "text": m["content"],
                                "trust": m["trust"], "score": m["score"]})
        prefs = self.ctx.memory.preferences(project)
        sys_prompt = build_system_prompt(
            personality=ov.get("personality", s.personality), length=ov.get("response_length", s.response_length),
            mode=ov.get("explain_mode", s.explain_mode), preferences=[p["content"] for p in prefs],
            summary=conv.get("summary", ""), state=conv.get("state") or {}, sources=sources, degraded=degraded,
            notes=notes)
        budget = max(512, s.context_window - s.max_tokens - estimate_tokens(sys_prompt) - estimate_tokens(user_text) - 64)
        hist = self._history(cid, budget)
        msgs = [{"role": "system", "content": sys_prompt}, *hist]
        return msgs, sources, {"memory_ids": [m["id"] for m in mem_hits], "degraded": degraded}

    # ------------------------------------------------------------------ remember / preference commands
    def _maybe_remember(self, cid: str, text: str) -> dict | None:
        m = _REMEMBER.match(text)
        kind = "fact"
        layer = "semantic"
        if not m:
            m = _PREF.match(text)
            kind, layer = "preference", "preference"
        if not m:
            return None
        content = m.group(1).strip().rstrip(".")
        if _PREF.match(text) is not None or re.search(r"\b(i prefer|i like|i want|please always|always|never)\b", content, re.I):
            kind, layer = "preference", "preference"
        mem = self.ctx.memory.add(layer, content, kind=kind, trust="user_verified", confidence=0.95, source_type="user_chat",
                                  source_ref=cid, conversation_id=cid, project_id=(self.get(cid) or {}).get("project_id", "default"))
        self.db.audit("user", "memory.remember", {"id": mem["id"], "layer": layer})
        return mem

    # ------------------------------------------------------------------ main entry point
    def send(self, cid: str, text: str, *, collections: list[str] | None = None, use_retrieval: bool = True,
             overrides: dict | None = None, attachments: list[dict] | None = None) -> Iterator[dict]:
        conv = self.get(cid)
        if not conv:
            yield {"type": "error", "error": {"code": "no_conversation", "message": "Conversation not found."}}
            return
        s = self.ctx.settings
        cancel = self._streams[cid] = threading.Event()
        try:
            user_msg = self.add_message(cid, "user", text, {"attachments": attachments or []})
            if attachments:
                self.set_state(cid, last_documents=[{"title": a.get("title") or a.get("name"), "document_id": a.get("document_id")}
                                                    for a in attachments])
            yield {"type": "meta", "conversation_id": cid, "user_message_id": user_msg["id"]}
            saved = self._maybe_remember(cid, text)
            msgs, sources, info = self.build_messages(cid, text, collections=collections, use_retrieval=use_retrieval, overrides=overrides)
            if saved:
                msgs[0]["content"] += (f"\n\nSYSTEM NOTE: the user just asked you to remember this and it HAS been saved "
                                       f"as a {saved['kind']} (verified by the user): \"{saved['content']}\". Acknowledge briefly.")
            if sources:
                yield {"type": "retrieval", "sources": [{k: v for k, v in x.items() if k != "text"} | {"snippet": x["text"][:300]} for x in sources],
                       "degraded": info["degraded"]}
            elif info["degraded"]:
                yield {"type": "notice", "message": info["degraded"]}
            out: list[str] = []
            usage: dict = {}
            model = ""
            try:
                for ev in self.ctx.provider.stream_chat(msgs, temperature=(overrides or {}).get("temperature", s.temperature),
                                                        top_p=s.top_p, max_tokens=(overrides or {}).get("max_tokens", s.max_tokens)):
                    if cancel.is_set() or self.ctx.stop_event.is_set():
                        out.append("")
                        yield {"type": "notice", "message": "Generation stopped."}
                        break
                    if ev.kind == "delta":
                        out.append(ev.text)
                        yield {"type": "delta", "text": ev.text}
                    elif ev.kind == "done" and ev.result:
                        usage, model = ev.result.usage, ev.result.model
            except LLMError as e:
                partial = "".join(out)
                if partial:
                    self.add_message(cid, "assistant", partial, {"partial": True, "error": e.to_dict()})
                yield {"type": "error", "error": e.to_dict()}
                return
            answer = "".join(out).strip()
            cited = sorted({int(n) for n in _CITE.findall(answer) if 0 < int(n) <= len(sources)})
            meta = {"sources": [{k: v for k, v in x.items() if k != "text"} | {"snippet": x["text"][:300]} for x in sources],
                    "cited": cited, "model": model, "usage": usage, "memory_ids": info["memory_ids"],
                    "chunk_ids": [x["chunk_id"] for x in sources if x["type"] == "document"], "stopped": cancel.is_set()}
            amsg = self.add_message(cid, "assistant", answer, meta)
            docs = list({x["title"]: {"title": x["title"], "source": x["source"]} for x in sources if x["type"] == "document"}.values())
            self.set_state(cid, last_result=answer[:500], **({"last_documents": docs} if docs else {}))
            yield {"type": "done", "message": amsg}
            # post-processing that must not block the answer
            for ev in self._post(cid):
                yield ev
        finally:
            self._streams.pop(cid, None)

    # ------------------------------------------------------------------ titles & rolling summary
    def _post(self, cid: str) -> Iterator[dict]:
        conv = self.get(cid)
        if not conv:
            return
        n = int(self.db.scalar("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,), 0))
        if conv["title"] in ("New conversation", "") and n >= 2:
            title = self._make_title(cid)
            self.db.update("conversations", cid, {"title": title})
            yield {"type": "title", "title": title}
        s = self.ctx.settings
        if n - (conv["summarized_upto"] or 0) > s.summarize_after:
            self.summarize(cid)

    def _make_title(self, cid: str) -> str:
        first = self.db.one("SELECT content FROM messages WHERE conversation_id=? AND role='user' ORDER BY seq LIMIT 1", (cid,))
        text = (first or {}).get("content", "Conversation")
        fallback = " ".join(text.split()[:7])[:60] or "Conversation"
        try:
            r = self.ctx.provider.chat([{"role": "system", "content": "Write a 3-6 word title for this conversation. Output only the title, no quotes."},
                                        {"role": "user", "content": text[:600]}], max_tokens=24, temperature=0.2)
            t = r.text.strip().strip('"').splitlines()[0][:70] if r.text.strip() else ""
            return t or fallback
        except Exception:  # noqa: BLE001 - titles are cosmetic
            return fallback

    def summarize(self, cid: str) -> str:
        """Fold older messages into the rolling summary so the prompt stays small."""
        conv = self.get(cid)
        if not conv:
            return ""
        keep = self.ctx.settings.history_messages
        msgs = self.messages(cid)
        old = [m for m in msgs[: max(0, len(msgs) - keep)] if m["seq"] > (conv["summarized_upto"] or 0)]
        if not old:
            return conv["summary"]
        transcript = "\n".join(f"{m['role']}: {m['content'][:500]}" for m in old)
        try:
            r = self.ctx.provider.chat([
                {"role": "system", "content": "You maintain a running summary of a conversation. Merge the new messages into the existing "
                                              "summary. Keep names, decisions, file/document names, open tasks and user preferences. Max 200 words."},
                {"role": "user", "content": f"Existing summary:\n{conv['summary'] or '(none)'}\n\nNew messages:\n{transcript}"}],
                max_tokens=400, temperature=0.2)
            summary = r.text.strip()
        except Exception:  # noqa: BLE001
            summary = ""
        if not summary:   # extractive fallback keeps working with no model
            bits = [m["content"].strip().split("\n")[0][:140] for m in old if m["role"] == "user"]
            summary = ((conv["summary"] + "\n") if conv["summary"] else "") + "\n".join("- " + b for b in bits)
            summary = summary[-1500:]
        self.db.update("conversations", cid, {"summary": summary, "summarized_upto": old[-1]["seq"]})
        return summary
