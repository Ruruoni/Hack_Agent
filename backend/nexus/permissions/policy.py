"""Execution permissions. Lives OUTSIDE the language model: the model can only *request* a tool call;
this module (driven by user-owned settings) decides allow / needs-approval / deny.

Invariants enforced here:
  * No tool exposes settings, permissions or approvals, so the model cannot raise its own privileges.
  * Approvals are decided only through the human-facing API (`decide_approval` rejects agent/model identities),
    are bound to a hash of the exact arguments, and are single-use.
  * Targets outside `authorized_targets` are denied even if a human would click approve.
"""
from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..database.db import Database, dumps, new_id, now

RISKS = ("low", "medium", "high", "critical")
# effects an action can have. Anything not listed in SAFE_* needs a human in approval/restricted mode.
READ = "read"
NET_READ = "network_read"
MEM_WRITE = "memory_write"
SCRATCH = "write_scratch"      # writes inside the agent's own workspace/reports area
WRITE = "write"
DELETE = "delete"
EXEC_SANDBOX = "exec_sandbox"
EXEC = "exec"
NET_WRITE = "network_write"
INSTALL = "install"
TRAIN = "train"
INGEST = "ingest"

NEVER_AUTO = {DELETE, EXEC, NET_WRITE, INSTALL, TRAIN, WRITE}
FORBIDDEN_FOR_AGENT = {"config", "permissions", "approve"}
PROTECTED_NAMES = {"nexus.db", "nexus.db-wal", "nexus.db-shm", "settings.json", ".env", "audit.jsonl"}


class PermissionDenied(Exception):
    pass


@dataclass
class Decision:
    outcome: str            # allow | approval | deny
    reason: str = ""


def _hash_args(tool: str, args: dict) -> str:
    return hashlib.sha256((tool + json.dumps(args, sort_keys=True, default=str)).encode()).hexdigest()


class PathPolicy:
    def __init__(self, settings: Settings):
        self.s = settings

    def scratch_roots(self) -> list[Path]:
        return [self.s.path(d).resolve() for d in ("workspace", "reports", "exports")]

    def read_roots(self) -> list[Path]:
        base = [self.s.path(d).resolve() for d in ("workspace", "reports", "exports", "uploads", "raw", "downloads", "web")]
        return base + [Path(p).expanduser().resolve() for p in self.s.allowed_roots]

    def resolve(self, path: str, write: bool = False) -> Path:
        """Resolve symlinks first, then require containment. Raises PermissionDenied."""
        if not path or "\x00" in path:
            raise PermissionDenied("invalid path")
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.s.path("workspace") / p
        rp = p.resolve()
        data_root = Path(self.s.data_dir).resolve()
        if rp.name in PROTECTED_NAMES:
            raise PermissionDenied(f"'{rp.name}' is protected application data")
        if rp == data_root or (data_root in rp.parents and rp.relative_to(data_root).parts[0] in (
                "logs", "vectors", "backups", "trash", "training", "extensions")):
            raise PermissionDenied("this area of the application data directory is not accessible to the agent")
        roots = self.read_roots()
        if not any(rp == r or r in rp.parents for r in roots):
            raise PermissionDenied(f"path is outside the allowed folders ({', '.join(str(r) for r in roots[:4])}...). "
                                   "A human can add folders under Settings > Allowed folders.")
        return rp

    def is_scratch(self, rp: Path) -> bool:
        return any(rp == r or r in rp.parents for r in self.scratch_roots())


class PermissionEngine:
    def __init__(self, settings: Settings, db: Database):
        self.s, self.db = settings, db
        self.paths = PathPolicy(settings)
        self._cv = threading.Condition()

    # ------------------------------------------------------------------ decisions
    def target_allowed(self, host: str) -> bool:
        host = host.strip().lower()
        for pat in self.s.authorized_targets:
            pat = pat.strip().lower()
            try:
                if "/" in pat and ipaddress.ip_address(host) in ipaddress.ip_network(pat, strict=False):
                    return True
            except ValueError:
                pass
            if host == pat or fnmatch.fnmatch(host, pat):
                return True
        return False

    def decide(self, name: str, effects: set[str], risk: str, args: dict, targets: list[str] | None = None,
               tainted: bool = False) -> Decision:
        if effects & FORBIDDEN_FOR_AGENT:
            return Decision("deny", "This capability can only be used by a human through the UI.")
        for t in targets or []:
            if not self.target_allowed(t):
                return Decision("deny", f"Target '{t}' is not in the authorized targets list. A human must add it under "
                                        "Settings > Authorized targets to confirm you are permitted to test it.")
        mode = self.s.execution_mode
        if mode == "read_only":
            ok = effects <= {READ} or (effects <= {READ, NET_READ} and self.s.read_only_allows_network)
            return Decision("allow") if ok else Decision("deny", "Read-only mode: this action would modify something or run code.")
        if mode == "approval_required":
            if effects <= {READ, NET_READ, MEM_WRITE, SCRATCH}:
                return Decision("approval", "Writing files, even in the workspace, is consequential in this mode") \
                    if (SCRATCH in effects and tainted) else Decision("allow")
            return Decision("approval", f"{'/'.join(sorted(effects))} requires your approval ({risk} risk).")
        # restricted automation
        # Listing a tool in auto_allow_tools is the human's explicit opt-in; sandboxed execution is the only 'high' risk effect it can unlock.
        auto_ok = (name in self.s.auto_allow_tools and not (effects & NEVER_AUTO)
                   and (risk in ("low", "medium") or effects <= {EXEC_SANDBOX})
                   and effects <= {READ, NET_READ, MEM_WRITE, SCRATCH, INGEST, EXEC_SANDBOX})
        if auto_ok and tainted and effects - {READ}:
            return Decision("approval", "Untrusted content with embedded instructions was seen in this task; confirm before acting.")
        if auto_ok:
            return Decision("allow")
        return Decision("approval", f"'{name}' is not on the automatic allow-list or has {'/'.join(sorted(effects))} effects.")

    # ------------------------------------------------------------------ approvals
    def request_approval(self, task_id: str | None, tool: str, args: dict, risk: str, reason: str, kind: str = "tool") -> str:
        aid = new_id("appr_")
        self.db.insert("approvals", {"id": aid, "task_id": task_id, "kind": kind, "tool": tool, "args": {"args": args, "hash": _hash_args(tool, args)},
                                     "risk": risk, "reason": reason, "status": "pending", "requested": now()})
        self.db.audit("agent", "approval.requested", {"id": aid, "tool": tool, "risk": risk, "task": task_id})
        with self._cv:
            self._cv.notify_all()
        return aid

    def decide_approval(self, aid: str, approve: bool, decided_by: str = "user", note: str = "") -> dict:
        if decided_by.lower() in ("agent", "model", "llm", "system", "assistant", "tool"):
            raise PermissionDenied("Approvals can only be decided by a human user.")
        a = self.db.get("approvals", aid)
        if not a:
            raise ValueError("no such approval")
        if a["status"] != "pending":
            raise ValueError(f"approval already {a['status']}")
        self.db.update("approvals", aid, {"status": "approved" if approve else "denied", "decided": now(),
                                          "decided_by": decided_by, "note": note})
        self.db.audit(decided_by, "approval.decided", {"id": aid, "approved": approve, "tool": a["tool"]})
        with self._cv:
            self._cv.notify_all()
        return self.db.get("approvals", aid)  # type: ignore[return-value]

    def wait_for_approval(self, aid: str, cancelled: Callable[[], bool], timeout: float | None = None) -> str:
        deadline = time.time() + (timeout or self.s.approval_timeout_s)
        while True:
            a = self.db.get("approvals", aid)
            if a and a["status"] != "pending":
                return a["status"]
            if cancelled():
                self.db.execute("UPDATE approvals SET status='cancelled', decided=? WHERE id=? AND status='pending'", (now(), aid))
                return "cancelled"
            if time.time() > deadline:
                self.db.execute("UPDATE approvals SET status='expired', decided=? WHERE id=? AND status='pending'", (now(), aid))
                return "expired"
            with self._cv:
                self._cv.wait(timeout=0.5)

    def consume(self, aid: str, tool: str, args: dict) -> bool:
        """Verify an approved record matches the exact call and burn it (single-use)."""
        a = self.db.get("approvals", aid)
        if not a or a["status"] != "approved" or a["args"]["hash"] != _hash_args(tool, args):
            return False
        self.db.update("approvals", aid, {"status": "used"})
        return True

    def pending(self) -> list[dict]:
        return self.db.query("SELECT * FROM approvals WHERE status='pending' ORDER BY requested")

    def history(self, limit: int = 100) -> list[dict]:
        return self.db.query("SELECT * FROM approvals ORDER BY requested DESC LIMIT ?", (limit,))
