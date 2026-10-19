"""Runs a tool call through: lookup -> schema validation -> permission decision -> (human approval) -> execution with
timeout -> output limiting/fencing -> verification, recording intended / attempted / verified states."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import jsonschema

from ..database.db import dumps, new_id, now
from ..knowledge.text import scan_injection
from ..permissions.policy import PermissionDenied
from .registry import Tool, ToolContext, truncate


@dataclass
class ToolOutcome:
    ok: bool
    state: str                 # denied | invalid | approval_denied | failed | completed | verified | cancelled
    data: dict
    run_id: str = ""
    verified: bool = False
    verification: str = ""
    flagged: list[str] | None = None
    message: str = ""

    def to_observation(self, limit: int = 4000) -> str:
        body = dumps(self.data)
        body, _ = truncate(body, limit)
        return body


class ToolExecutor:
    def __init__(self, app):
        self.app = app

    def execute(self, name: str, args: dict, ctx: ToolContext, tainted: bool = False,
                on_approval: Any = None) -> ToolOutcome:
        db, perms, reg = self.app.db, self.app.perms, self.app.tools
        run_id = new_id("run_")
        tool = reg.get(name)
        db.insert("tool_runs", {"id": run_id, "task_id": ctx.task_id, "tool": name, "args": args, "state": "intended",
                                "created": now(), "updated": now()})

        def finish(state: str, data: dict, **kw) -> ToolOutcome:
            db.update("tool_runs", run_id, {"state": state, "result": dumps(data)[:20000], "updated": now(),
                                            "verified": 1 if kw.get("verified") else 0, "verification": kw.get("verification", ""),
                                            "error": data.get("error", "") if isinstance(data, dict) else ""})
            return ToolOutcome(ok=state in ("completed", "verified"), state=state, data=data, run_id=run_id, **kw)

        if tool is None:
            return finish("invalid", {"ok": False, "error": f"Unknown tool '{name}'. Available: " + ", ".join(t.name for t in reg.available())})
        miss = tool.missing_dependencies()
        if miss:
            return finish("invalid", {"ok": False, "error": f"Tool '{name}' is missing dependencies: {miss}. Propose an extension or install them (needs approval)."})
        try:
            jsonschema.validate(args, tool.input_schema)
        except jsonschema.ValidationError as e:
            return finish("invalid", {"ok": False, "error": f"Invalid arguments for {name}: {e.message}"})
        try:
            effects = tool.effect_set(ctx, args)
            targets = tool.targets_fn(args) if tool.targets_fn else []
        except PermissionDenied as e:
            db.audit("agent", "tool.denied", {"tool": name, "why": str(e)})
            return finish("denied", {"ok": False, "error": f"Permission denied: {e}"})
        decision = perms.decide(name, effects, tool.risk, args, targets, tainted)
        if decision.outcome == "deny":
            db.audit("agent", "tool.denied", {"tool": name, "why": decision.reason, "task": ctx.task_id})
            return finish("denied", {"ok": False, "error": f"Permission denied: {decision.reason}"})
        if decision.outcome == "approval":
            db.update("tool_runs", run_id, {"state": "awaiting_approval", "updated": now()})
            aid = perms.request_approval(ctx.task_id, name, args, tool.risk, decision.reason)
            self.app.bus.publish("approval.requested", {"id": aid, "tool": name, "task": ctx.task_id})
            if on_approval:
                on_approval(aid)
            status = perms.wait_for_approval(aid, lambda: ctx.cancelled() or self.app.stop_event.is_set())
            if status != "approved" or not perms.consume(aid, name, args):
                msg = {"denied": "The user denied this action.", "expired": "Approval timed out.", "cancelled": "Cancelled."}.get(status, "Approval was not granted.")
                return finish("approval_denied" if status != "cancelled" else "cancelled", {"ok": False, "error": msg, "approval": status})
        if ctx.cancelled() or self.app.stop_event.is_set():
            return finish("cancelled", {"ok": False, "error": "Cancelled before execution."})
        db.update("tool_runs", run_id, {"state": "attempted", "updated": now()})
        db.audit("agent", "tool.attempt", {"tool": name, "args": dumps(args)[:500], "task": ctx.task_id, "risk": tool.risk})
        result: dict[str, Any] = {}
        err: list[BaseException] = []

        def work() -> None:
            try:
                result.update(tool.handler(ctx, args))
            except PermissionDenied as e:
                result.update({"ok": False, "error": f"Permission denied: {e}", "_denied": True})
            except Exception as e:  # noqa: BLE001
                err.append(e)

        th = threading.Thread(target=work, daemon=True, name=f"tool-{name}")
        th.start()
        t0 = time.time()
        timeout = min(tool.timeout, self.app.settings.tool_timeout_s * 10)
        while th.is_alive():
            th.join(0.1)
            if ctx.cancelled() or self.app.stop_event.is_set():
                return finish("cancelled", {"ok": False, "error": "Cancelled while running."})
            if time.time() - t0 > timeout:
                return finish("failed", {"ok": False, "error": f"Timed out after {timeout}s."})
        if err:
            e = err[0]
            return finish("failed", {"ok": False, "error": f"{e.__class__.__name__}: {e}"})
        if not isinstance(result, dict) or "ok" not in result:
            result = {"ok": True, "result": result}
        flagged: list[str] = []
        if tool.untrusted_output and result.get("ok"):
            flagged = scan_injection(dumps(result))
            if flagged:
                db.audit("system", "injection.detected", {"tool": name, "patterns": flagged, "task": ctx.task_id})
        if result.pop("_denied", False):
            db.audit("agent", "tool.denied", {"tool": name, "why": result.get("error"), "task": ctx.task_id})
            return finish("denied", result)
        if not result.get("ok"):
            return finish("failed", result)
        verified, why = False, "no verifier for this tool: result is reported as completed, not independently verified"
        if tool.verify_fn:
            try:
                verified, why = tool.verify_fn(ctx, args, result)
            except Exception as e:  # noqa: BLE001
                verified, why = False, f"verification error: {e}"
        out = finish("verified" if verified else "completed", result, verified=verified, verification=why, flagged=flagged or None)
        out.ok = True
        return out
