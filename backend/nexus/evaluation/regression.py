"""Regression suite. Deterministic checks (retrieval, prompt contents, workflow discoverability/replay) run without any
model; optional `llm_answer` checks need LM Studio and are reported as SKIPPED (never as passed) when it is unreachable."""
from __future__ import annotations

import time

from ..database.db import new_id, now
from ..llm.base import LLMError

KINDS = ("retrieval", "prompt_contains", "tool_selection", "workflow_replay", "llm_answer")


class EvaluationManager:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    # ---- cases -------------------------------------------------------------------------
    def add_case(self, name: str, kind: str, spec: dict, origin: str = "") -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        cid = new_id("ev_")
        self.db.insert("eval_cases", {"id": cid, "name": name, "kind": kind, "spec": spec, "origin": origin, "active": 1, "created": now()})
        return self.db.get("eval_cases", cid)  # type: ignore[return-value]

    def list_cases(self, active_only: bool = False) -> list[dict]:
        return self.db.query("SELECT * FROM eval_cases" + (" WHERE active=1" if active_only else "") + " ORDER BY created")

    def set_active(self, cid: str, active: bool) -> None:
        self.db.update("eval_cases", cid, {"active": 1 if active else 0})

    def delete_case(self, cid: str) -> None:
        self.db.execute("DELETE FROM eval_cases WHERE id=?", (cid,))

    # ---- execution ---------------------------------------------------------------------
    def run_case(self, case: dict) -> dict:
        spec, kind = case["spec"], case["kind"]
        t0 = time.time()
        try:
            status, detail = getattr(self, f"_check_{kind}")(spec)
        except LLMError as e:
            status, detail = "skipped", f"model unavailable: {e.message}"
        except Exception as e:  # noqa: BLE001
            status, detail = "fail", f"check crashed: {e.__class__.__name__}: {e}"
        return {"case_id": case["id"], "name": case["name"], "kind": kind, "status": status, "detail": detail, "ms": int((time.time() - t0) * 1000)}

    def run_suite(self, label: str = "manual") -> dict:
        results = [self.run_case(c) for c in self.list_cases(active_only=True)]
        prev = self.db.one("SELECT * FROM eval_runs ORDER BY ts DESC LIMIT 1")
        summary = {"label": label, "total": len(results), "passed": sum(r["status"] == "pass" for r in results),
                   "failed": sum(r["status"] == "fail" for r in results), "skipped": sum(r["status"] == "skipped" for r in results), "ts": now()}
        if prev:
            before = {r["case_id"]: r["status"] for r in prev["results"]}
            summary["regressions"] = [r["name"] for r in results if r["status"] == "fail" and before.get(r["case_id"]) == "pass"]
        rid = new_id("er_")
        self.db.insert("eval_runs", {"id": rid, "ts": summary["ts"], "summary": summary, "results": results})
        self.app.bus.publish("eval.run", {"id": rid, **summary})
        return {"id": rid, **summary, "results": results}

    def runs(self, limit: int = 20) -> list[dict]:
        return self.db.query("SELECT id, ts, summary FROM eval_runs ORDER BY ts DESC LIMIT ?", (limit,))

    # ---- check implementations -------------------------------------------------------------
    def _check_retrieval(self, spec: dict) -> tuple[str, str]:
        k = int(spec.get("k", 5))
        if spec.get("expect_memory_ids"):
            ids = {m["id"] for m in self.app.memory.search(spec["query"], k=k, project_id=spec.get("project_id"))}
            miss = [m for m in spec["expect_memory_ids"] if m not in ids]
            # an expected memory that was legitimately superseded/outdated is not a regression of retrieval
            miss = [m for m in miss if (self.app.memory.get(m) or {"status": "deleted"}).get("status") not in ("outdated",)]
            return ("pass", "expected memories retrieved") if not miss else ("fail", f"memories not retrieved in top-{k}: {miss}")
        if spec.get("expect_text"):
            res = self.app.kb.search(spec["query"], [spec["collection"]] if spec.get("collection") else None, k=k)
            ok = any(spec["expect_text"].lower() in h["text"].lower() for h in res["hits"])
            return ("pass", "expected text retrieved") if ok else ("fail", f"'{spec['expect_text']}' not in top-{k} results")
        return "fail", "retrieval case has no expectation"

    def _check_prompt_contains(self, spec: dict) -> tuple[str, str]:
        from ..agent.chat import ChatService
        chat = ChatService(self.app)
        conv = chat.create(title="__eval__", project_id=spec.get("project_id", "default"))
        try:
            msgs, _, _ = chat.build_messages(conv["id"], spec.get("query", "hello"), collections=None, use_retrieval=False)
        finally:
            chat.delete(conv["id"])
        return ("pass", "present in system prompt") if spec["text"].lower() in msgs[0]["content"].lower() else ("fail", "text missing from system prompt")

    def _check_tool_selection(self, spec: dict) -> tuple[str, str]:
        names = [s["name"] for s in self.app.skills.match(spec["objective"], k=3)]
        return ("pass", f"matched {names}") if spec["expect_skill"] in names else ("fail", f"expected skill '{spec['expect_skill']}', matched {names}")

    def _check_workflow_replay(self, spec: dict) -> tuple[str, str]:
        sk = self.app.skills.get(spec["skill_name"])
        if not sk or sk["status"] != "approved":
            return "fail", "skill is no longer approved"
        errs = self.app.skills.validate_spec(sk["spec"])
        if errs:
            return "fail", "; ".join(errs)
        if spec.get("params") is not None:
            r = self.app.skills._replay(sk["spec"], spec["params"])
            return ("pass", "replayed read-only steps OK") if r["ok"] else ("fail", str(r["log"]))
        return "pass", "spec valid"

    def _check_llm_answer(self, spec: dict) -> tuple[str, str]:
        res = self.app.provider.chat([{"role": "user", "content": spec["question"]}], max_tokens=300, temperature=0)
        missing = [w for w in spec.get("must_include", []) if w.lower() not in res.text.lower()]
        return ("pass", "answer contains required phrases") if not missing else ("fail", f"answer missing {missing}")
