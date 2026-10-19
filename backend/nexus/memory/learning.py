"""Experience-based learning: feedback & corrections, task outcomes, retrieval utility, reusable workflow candidates.

This is NOT model training. Nothing here touches model weights. The three systems are reported separately by `overview()`.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..database.db import new_id, now

SCOPES = ("one_time", "preference", "fact", "hypothesis")
_PREF = re.compile(r"\b(always|never|from now on|going forward|in future|every time|i prefer|please (don't|do not|stop)|whenever)\b", re.I)
_ONCE = re.compile(r"\b(just this once|this time|for this (question|answer|message|case)|only here)\b", re.I)
_FACT = re.compile(r"\b(is actually|is wrong|incorrect|the correct|should be|not true|isn't|is not|are not)\b", re.I)


def infer_scope(correction: str) -> tuple[str, bool]:
    """(scope, confident). When not confident the caller must ASK the user what the scope is."""
    if _ONCE.search(correction):
        return "one_time", True
    if _PREF.search(correction):
        return "preference", True
    if _FACT.search(correction):
        return "fact", False
    return "one_time", False


class LearningManager:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    # ------------------------------------------------------------------ feedback
    def submit_feedback(self, conversation_id: str, message_id: str | None = None, rating: int | None = None, correction: str = "",
                        scope: str | None = None, replaces_memory_id: str | None = None) -> dict:
        msg = self.db.get("messages", message_id) if message_id else None
        conv = self.db.get("conversations", conversation_id)
        if not conv:
            raise ValueError("no such conversation")
        out: dict[str, Any] = {"saved": None, "regression_case": None, "utility": {}}
        if rating is not None and msg:
            delta = 0.5 if rating > 0 else -0.5
            meta = msg["meta"] or {}
            self.app.kb.record_utility(meta.get("chunk_ids", []), delta)
            self.app.memory.touch([m for m in meta.get("memory_ids", [])], delta)
            out["utility"] = {"chunks": len(meta.get("chunk_ids", [])), "memories": len(meta.get("memory_ids", [])), "delta": delta}
        mem = None
        if correction.strip():
            if scope is None:
                guess, confident = infer_scope(correction)
                if not confident:
                    return {"needs_scope": True, "suggested": guess, "options": list(SCOPES),
                            "question": "Is this a one-time correction for this answer, a general preference from now on, a verified fact, or just a hypothesis?"}
                scope = guess
            if scope not in SCOPES:
                raise ValueError(f"scope must be one of {SCOPES}")
            question = self._question_before(msg)
            mem = self._save_correction(conv, correction.strip(), scope, replaces_memory_id, msg)
            out["saved"] = {"id": mem["id"], "layer": mem["layer"], "kind": mem["kind"], "trust": mem["trust"], "scope": scope}
            if scope in ("preference", "fact") and question is not None:
                case = self._regression_for(mem, question, scope, correction.strip())
                out["regression_case"] = case["id"]
            elif scope == "preference":
                case = self.app.evaluation.add_case(f"preference: {correction[:50]}", "prompt_contains", {"text": correction.strip()[:120], "project_id": conv["project_id"]},
                                                    origin=f"feedback:{mem['id']}")
                out["regression_case"] = case["id"]
            skill_id = (msg["meta"] or {}).get("skill_id") if msg else None
            if skill_id:
                self.db.execute("UPDATE skills SET failures=failures+1 WHERE id=?", (skill_id,))
                self.app.memory.add("procedural", f"Lesson from user correction (skill {skill_id}): {correction.strip()}", kind="correction",
                                    trust="user_verified", source_type="feedback", source_ref=skill_id)
                out["skill_flagged"] = skill_id
        self.db.insert("feedback", {"id": new_id("fb_"), "conversation_id": conversation_id, "message_id": message_id, "rating": rating,
                                    "correction": correction, "scope": scope, "memory_id": mem["id"] if mem else None, "created": now()})
        self.db.audit("user", "feedback", {"conversation": conversation_id, "rating": rating, "scope": scope})
        self.app.bus.publish("learning.feedback", {"scope": scope, "memory": mem["id"] if mem else None})
        return out

    def _question_before(self, msg: dict | None) -> str | None:
        if not msg:
            return None
        r = self.db.one("SELECT content FROM messages WHERE conversation_id=? AND seq<? AND role='user' ORDER BY seq DESC LIMIT 1",
                        (msg["conversation_id"], msg["seq"]))
        return r["content"] if r else None

    def _save_correction(self, conv: dict, text: str, scope: str, replaces: str | None, msg: dict | None) -> dict:
        common = dict(trust="user_verified", confidence=0.95, source_type="user_feedback", source_ref=msg["id"] if msg else "",
                      conversation_id=conv["id"], project_id=conv["project_id"])
        if scope == "one_time":
            return self.app.memory.add("episodic", text, kind="correction", meta={"scope": "one_time"}, **common)
        if scope == "preference":
            return self.app.memory.add("preference", text, kind="preference", meta={"scope": "preference"}, **common)
        if scope == "fact":
            return self.app.memory.add("semantic", text, kind="fact", supersedes=replaces, meta={"scope": "fact"}, **common)
        common.update(trust="hypothesis", confidence=0.3)
        return self.app.memory.add("semantic", text, kind="hypothesis", meta={"scope": "hypothesis"}, **common)

    def _regression_for(self, mem: dict, question: str, scope: str, correction: str) -> dict:
        if scope == "preference":
            return self.app.evaluation.add_case(f"preference kept: {correction[:50]}", "prompt_contains",
                                                {"text": correction[:120], "query": question, "project_id": mem["project_id"]}, origin=f"feedback:{mem['id']}")
        return self.app.evaluation.add_case(f"corrected fact retrievable: {question[:50]}", "retrieval",
                                            {"query": question, "expect_memory_ids": [mem["id"]], "k": 5, "project_id": mem["project_id"]}, origin=f"feedback:{mem['id']}")

    # ------------------------------------------------------------------ task outcomes
    def on_task_finished(self, task: dict | None) -> None:
        if not task:
            return
        runs = self.db.query("SELECT * FROM tool_runs WHERE task_id=? ORDER BY created", (task["id"],))
        ok_runs = [r for r in runs if r["state"] in ("completed", "verified")]
        bad = [r for r in runs if r["state"] in ("failed", "denied", "approval_denied", "invalid")]
        success = task["status"] == "completed" and not (runs and not ok_runs)
        tools = [r["tool"] for r in runs]
        errors = sorted({(r["error"] or r["state"])[:100] for r in bad})
        content = (f"Task: {task['objective'][:200]} | Approach: {' -> '.join(tools) or 'no tools'} | "
                   f"Outcome: {'success' if success else 'failure (' + task['status'] + ')'}"
                   + (f" | Errors: {'; '.join(errors)[:300]}" if errors else ""))
        self.app.memory.add("episodic", content, kind="episode", trust="unverified", confidence=0.7 if success else 0.4, source_type="task",
                            source_ref=task["id"], project_id=task["project_id"], meta={"tools": tools, "success": success, "errors": errors})
        delta = 0.2 if success else -0.2
        self.app.memory.touch((task["state"] or {}).get("memory_ids", []), delta)
        distinct = list(dict.fromkeys(r["tool"] for r in ok_runs))
        tainted = bool((task["state"] or {}).get("tainted"))
        if success and len(ok_runs) >= 2 and not tainted and "run_skill" not in tools and not any(r["tool"] in ("python_sandbox",) for r in ok_runs):
            try:
                self.propose_workflow(task, ok_runs)
            except Exception:  # noqa: BLE001 - learning must never break task completion
                pass

    def record_outcome(self, task_id: str, success: bool, note: str = "") -> dict:
        """Follow-up evaluation from the user ('did that actually work?') - the verified signal learning relies on."""
        t = self.db.get("tasks", task_id)
        if not t:
            raise ValueError("no such task")
        mem = self.app.memory.add("episodic", f"User follow-up on task '{t['objective'][:120]}': {'it worked' if success else 'it did NOT work'}. {note}".strip(),
                                  kind="episode", trust="user_verified", confidence=0.95, source_type="user_followup", source_ref=task_id, project_id=t["project_id"])
        self.app.memory.touch((t["state"] or {}).get("memory_ids", []), 0.5 if success else -0.5)
        self.db.audit("user", "task.outcome", {"task": task_id, "success": success})
        return mem

    # ------------------------------------------------------------------ workflow learning
    def propose_workflow(self, task: dict, runs: list[dict]) -> dict | None:
        obj = task["objective"]
        steps, params, examples = [], {}, {}
        by_value: dict[str, str] = {}          # identical literal values share one parameter
        for i, r in enumerate(runs):
            new_args = {}
            for k, v in dict(r["args"] or {}).items():
                if isinstance(v, str) and len(v) >= 3 and v in obj and not v.startswith(("http://", "https://")):
                    if v not in by_value:
                        pname = k if k not in params else f"{k}{i}"
                        params[pname] = {"type": "string", "description": f"{k} used in the original task"}
                        examples[pname] = v
                        by_value[v] = pname
                    new_args[k] = "{{params.%s}}" % by_value[v]
                else:
                    new_args[k] = v
            steps.append({"id": f"s{i + 1}", "tool": r["tool"], "args": new_args})
        sig = "->".join(s["tool"] for s in steps)
        name = "wf_" + re.sub(r"[^a-z0-9]+", "_", " ".join(re.findall(r"[a-z]{3,}", obj.lower())[:4]))[:30].strip("_")
        if len(name) < 6:
            name = "wf_" + sig.replace("->", "_")[:30]
        if any(json.dumps([s["tool"] for s in sk["spec"].get("steps", [])]) == json.dumps([s["tool"] for s in steps]) and sk["name"] == name
               for sk in self.app.skills.list("workflow")):
            return None
        spec = {"name": name, "description": f"Learned from task: {obj[:160]}", "params": params, "steps": steps, "keywords": re.findall(r"[a-z]{4,}", obj.lower())[:8],
                "examples": [{"params": examples}] if examples or not params else []}
        sk = self.app.skills.create_candidate(spec, origin=f"task:{task['id']}")
        if sk["status"] == "candidate":
            sk = self.app.skills.evaluate(sk["id"])        # read-only replay + regression suite; a human still has to approve
        self.app.bus.publish("learning.workflow_candidate", {"skill": sk["id"], "status": sk["status"]})
        return sk

    # ------------------------------------------------------------------ the three systems, kept apart
    def overview(self) -> dict:
        db = self.db
        reg = db.one("SELECT * FROM model_registry WHERE status='active' AND kind='adapter' LIMIT 1")
        last_run = db.one("SELECT summary FROM eval_runs ORDER BY ts DESC LIMIT 1")
        return {
            "data_ingestion": {"what": "Adds searchable documents/records to the knowledge base. Does not change the model.",
                               "collections": db.scalar("SELECT COUNT(*) FROM collections", (), 0), "documents": db.scalar("SELECT COUNT(*) FROM documents", (), 0),
                               "chunks_indexed": db.scalar("SELECT COUNT(*) FROM chunks WHERE status='indexed'", (), 0),
                               "quarantined": db.scalar("SELECT COUNT(*) FROM chunks WHERE status='quarantined'", (), 0),
                               "jobs_running": db.scalar("SELECT COUNT(*) FROM ingestion_jobs WHERE status='running'", (), 0)},
            "persistent_learning": {"what": "Remembers facts, preferences, corrections, past tasks and reusable workflows. Does not change the model.",
                                    "memories": self.app.memory.counts(), "feedback_items": db.scalar("SELECT COUNT(*) FROM feedback", (), 0),
                                    "workflows_approved": db.scalar("SELECT COUNT(*) FROM skills WHERE kind='workflow' AND status='approved'", (), 0),
                                    "workflow_candidates": db.scalar("SELECT COUNT(*) FROM skills WHERE kind='workflow' AND status IN ('candidate','validated')", (), 0),
                                    "regression_cases": db.scalar("SELECT COUNT(*) FROM eval_cases WHERE active=1", (), 0), "last_regression_run": last_run["summary"] if last_run else None},
            "model_weights": {"what": "Fine-tuning changes the actual model weights. Optional, separate, requires explicit approval.",
                              "weights_modified": reg is not None, "active_adapter": reg["name"] if reg else None,
                              "training_runs": db.scalar("SELECT COUNT(*) FROM training_runs", (), 0),
                              "statement": ("The base model is unmodified. All learning so far is retrieval/memory based." if reg is None
                                            else f"An accepted fine-tuned adapter '{reg['name']}' is registered as active.")}}
