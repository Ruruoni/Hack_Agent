"""Reusable skills (declarative workflows), extension proposals and the approve/test/version workflow.

Safety model:
  * Workflow skills are DATA (a list of tool calls). Running one still sends every step through the permission engine.
  * The agent may create *candidates* and *proposals*; only a human can approve (activate) them.
  * Extension (plugin code) must pass an isolated self-test before a human can activate it. Dependencies are never
    installed automatically.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from ..database.db import new_id, now
from ..tools.registry import Tool, ToolContext, fail, ok

STATUSES = ("candidate", "validated", "failed_validation", "approved", "disabled", "superseded", "proposed", "tested", "test_failed", "active")
_TPL = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
PKG_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,60}(\[[A-Za-z0-9,_\-]+\])?([=<>!~]=?[A-Za-z0-9.*]+)?$")


def _lookup(path: str, params: dict, steps: dict):
    root, *rest = path.split(".")
    cur = params if root == "params" else steps if root == "steps" else None
    if cur is None:
        raise KeyError(path)
    for part in rest:
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    return cur


def render(value, params: dict, steps: dict):
    if isinstance(value, str):
        m = _TPL.fullmatch(value.strip())
        if m:
            return _lookup(m.group(1), params, steps)
        return _TPL.sub(lambda mm: str(_lookup(mm.group(1), params, steps)), value)
    if isinstance(value, dict):
        return {k: render(v, params, steps) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, params, steps) for v in value]
    return value


class SkillManager:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    # ------------------------------------------------------------------ workflow skills
    def validate_spec(self, spec: dict) -> list[str]:
        errs: list[str] = []
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", str(spec.get("name", ""))):
            errs.append("name must be snake_case (3-41 chars)")
        steps = spec.get("steps")
        if not isinstance(steps, list) or not steps:
            return errs + ["workflow needs at least one step"]
        if len(steps) > 12:
            errs.append("a workflow may have at most 12 steps")
        declared = set((spec.get("params") or {}).keys())
        ids: set[str] = set()
        for i, st in enumerate(steps):
            sid = st.get("id") or f"s{i + 1}"
            if sid in ids:
                errs.append(f"duplicate step id '{sid}'")
            ids.add(sid)
            t = self.app.tools.get(st.get("tool", ""))
            if t is None:
                errs.append(f"step {sid}: unknown tool '{st.get('tool')}'")
                continue
            if t.name in ("run_skill",):
                errs.append(f"step {sid}: workflows cannot call run_skill (no recursion)")
            blob = json.dumps(st.get("args", {}))
            for ref in _TPL.findall(blob):
                kind, _, rest = ref.partition(".")
                if kind == "params" and rest.split(".")[0] not in declared:
                    errs.append(f"step {sid}: references undeclared parameter '{rest}'")
                elif kind == "steps" and rest.split(".")[0] not in ids:
                    errs.append(f"step {sid}: references step '{rest.split('.')[0]}' which has not run yet")
                elif kind not in ("params", "steps"):
                    errs.append(f"step {sid}: bad template '{{{{{ref}}}}}'")
        return errs

    def create_candidate(self, spec: dict, origin: str = "agent", description: str = "") -> dict:
        errs = self.validate_spec(spec)
        sid = new_id("sk_")
        ver = 1 + int(self.db.scalar("SELECT COALESCE(MAX(version),0) FROM skills WHERE name=? AND kind='workflow'", (spec.get("name", ""),), 0))
        self.db.insert("skills", {"id": sid, "name": spec.get("name", "unnamed"), "version": ver, "kind": "workflow",
                                  "status": "candidate" if not errs else "failed_validation",
                                  "description": description or spec.get("description", ""), "spec": spec, "origin": origin,
                                  "tests": {"static_errors": errs} if errs else {}, "created": now(), "updated": now()})
        self.db.audit("agent" if origin.startswith("task:") else "user", "skill.candidate", {"id": sid, "name": spec.get("name"), "origin": origin})
        return self.db.get("skills", sid)  # type: ignore[return-value]

    def list(self, kind: str | None = None, status: str | None = None) -> list[dict]:
        cond, p = [], []
        for c, v in (("kind", kind), ("status", status)):
            if v:
                cond.append(f"{c}=?")
                p.append(v)
        return self.db.query("SELECT * FROM skills" + (" WHERE " + " AND ".join(cond) if cond else "") + " ORDER BY name, version DESC", p)

    def get(self, sid: str) -> dict | None:
        return self.db.get("skills", sid) or self.db.one("SELECT * FROM skills WHERE name=? AND status='approved' ORDER BY version DESC", (sid,))

    def evaluate(self, sid: str) -> dict:
        """Static validation + read-only replay of the skill's examples + the global regression suite."""
        sk = self.db.get("skills", sid)
        if not sk or sk["kind"] != "workflow":
            raise ValueError("no such workflow skill")
        spec = sk["spec"]
        report: dict = {"static_errors": self.validate_spec(spec), "replays": [], "regression": None}
        if not report["static_errors"]:
            for ex in (spec.get("examples") or [])[:3]:
                report["replays"].append(self._replay(spec, ex.get("params", {})))
            ev = getattr(self.app, "evaluation", None)
            if ev:
                report["regression"] = ev.run_suite(label=f"skill:{sk['name']}:v{sk['version']}")
        replay_ok = all(r["ok"] for r in report["replays"])
        reg_ok = report["regression"] is None or report["regression"]["failed"] == 0
        passed = not report["static_errors"] and replay_ok and reg_ok
        report["passed"] = passed
        report["note"] = ("No examples were provided, so only static validation and the regression suite ran." if not spec.get("examples") else
                          "Examples were replayed with read-only tools only; steps with side effects were skipped, not executed.")
        self.db.update("skills", sid, {"tests": report, "status": "validated" if passed else "failed_validation", "updated": now()})
        return self.db.get("skills", sid)  # type: ignore[return-value]

    def _replay(self, spec: dict, params: dict) -> dict:
        steps_out: dict = {}
        log = []
        mode_effects = {"read"}
        ctx = ToolContext(app=self.app, task_id=None)
        for i, st in enumerate(spec["steps"]):
            sid = st.get("id") or f"s{i + 1}"
            tool = self.app.tools.get(st["tool"])
            try:
                args = render(st.get("args", {}), params, steps_out)
                eff = tool.effect_set(ctx, args)  # type: ignore[union-attr]
            except Exception as e:  # noqa: BLE001
                log.append({"step": sid, "ok": False, "error": f"cannot render/inspect: {e}"})
                return {"ok": False, "log": log}
            if not eff <= mode_effects:
                log.append({"step": sid, "skipped": f"has side effects {sorted(eff)} - not executed during evaluation"})
                steps_out[sid] = {"skipped": True}
                continue
            try:
                out = tool.handler(ctx, args)  # type: ignore[union-attr]
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
            steps_out[sid] = out
            log.append({"step": sid, "ok": bool(out.get("ok")), "error": out.get("error")})
            if not out.get("ok"):
                return {"ok": False, "log": log}
        return {"ok": True, "log": log}

    def approve(self, sid: str, by: str = "user") -> dict:
        if by.lower() in ("agent", "model", "llm", "system"):
            raise PermissionError("Only a human can approve a skill.")
        sk = self.db.get("skills", sid)
        if not sk or sk["kind"] != "workflow":
            raise ValueError("no such workflow skill")
        if sk["status"] != "validated":
            raise ValueError(f"skill must pass evaluation first (status is '{sk['status']}')")
        self.db.execute("UPDATE skills SET status='superseded', updated=? WHERE name=? AND status='approved' AND kind='workflow'", (now(), sk["name"]))
        self.db.update("skills", sid, {"status": "approved", "updated": now()})
        self.db.audit(by, "skill.approve", {"id": sid, "name": sk["name"], "version": sk["version"]})
        return self.db.get("skills", sid)  # type: ignore[return-value]

    def set_status(self, sid: str, status: str, by: str = "user") -> None:
        if status not in ("disabled", "approved") or by.lower() in ("agent", "model"):
            raise PermissionError("not allowed")
        self.db.update("skills", sid, {"status": status, "updated": now()})
        self.db.audit(by, f"skill.{status}", {"id": sid})

    def versions(self, name: str) -> list[dict]:
        return self.db.query("SELECT id,name,version,status,origin,successes,failures,created,updated FROM skills WHERE name=? ORDER BY version DESC", (name,))

    def match(self, objective: str, k: int = 3) -> list[dict]:
        toks = set(re.findall(r"[a-z0-9]{3,}", objective.lower()))
        scored = []
        for sk in self.list("workflow", "approved"):
            words = set(re.findall(r"[a-z0-9]{3,}", (sk["name"].replace("_", " ") + " " + sk["description"] + " " + " ".join(sk["spec"].get("keywords", []))).lower()))
            overlap = len(toks & words)
            if overlap:
                rate = (sk["successes"] + 1) / (sk["successes"] + sk["failures"] + 2)
                scored.append((overlap * rate, sk))
        return [s for _, s in sorted(scored, key=lambda x: -x[0])[:k]]

    def run(self, ref: str, params: dict, ctx: ToolContext, tainted: bool = False) -> dict:
        sk = self.get(ref)
        if not sk or sk["status"] != "approved":
            return fail(f"no approved skill named '{ref}'")
        spec = sk["spec"]
        missing = [p for p, d in (spec.get("params") or {}).items() if p not in params and not (isinstance(d, dict) and "default" in d)]
        if missing:
            return fail(f"missing parameters: {missing}")
        full = {p: (params[p] if p in params else d.get("default")) for p, d in (spec.get("params") or {}).items()}
        steps_out: dict = {}
        log = []
        for i, st in enumerate(spec["steps"]):
            sid = st.get("id") or f"s{i + 1}"
            try:
                args = render(st.get("args", {}), full, steps_out)
            except (KeyError, IndexError, TypeError) as e:
                self.db.execute("UPDATE skills SET failures=failures+1 WHERE id=?", (sk["id"],))
                return fail(f"step {sid}: cannot resolve template ({e})", log=log)
            out = self.app.executor.execute(st["tool"], args, ctx, tainted=tainted)
            steps_out[sid] = out.data
            log.append({"step": sid, "tool": st["tool"], "state": out.state, "ok": out.ok, "verified": out.verified})
            if not out.ok:
                self.db.execute("UPDATE skills SET failures=failures+1 WHERE id=?", (sk["id"],))
                return fail(f"step {sid} ({st['tool']}) {out.state}: {out.data.get('error', '')}", log=log)
        self.db.execute("UPDATE skills SET successes=successes+1 WHERE id=?", (sk["id"],))
        last = steps_out[list(steps_out)[-1]]
        return ok(skill=sk["name"], version=sk["version"], log=log, result=last)

    # ------------------------------------------------------------------ extension proposals (plugin code)
    def propose_extension(self, capability: str, rationale: str = "", origin: str = "agent", code: str | None = None,
                          dependencies: list[str] | None = None) -> dict:
        name = re.sub(r"[^a-z0-9_]", "_", capability.lower())[:40].strip("_") or "extension"
        sid = new_id("ext_")
        folder = self.app.settings.path("extensions", "proposed", sid)
        folder.mkdir(parents=True, exist_ok=True)
        deps = [d for d in (dependencies or []) if PKG_RX.match(d)]
        code = code or self._template(name, capability, deps)
        (folder / f"{name}.py").write_text(code, encoding="utf-8")
        (folder / "PROPOSAL.md").write_text(
            f"# Extension proposal: {capability}\n\n**Why:** {rationale or 'A task needed a capability no registered tool provides.'}\n\n"
            f"**Declared Python dependencies:** {', '.join(deps) or 'none'}\n\n"
            "**Status:** PROPOSAL ONLY. Nothing here runs until you (1) run the isolated self-test, (2) install any dependencies yourself or "
            "via the approval button, and (3) approve activation in Skills > Extensions.\n", encoding="utf-8")
        self.db.insert("skills", {"id": sid, "name": name, "version": 1, "kind": "extension", "status": "proposed", "description": capability,
                                  "spec": {"file": str(folder / f"{name}.py"), "dependencies": deps, "rationale": rationale}, "origin": origin,
                                  "tests": {}, "created": now(), "updated": now()})
        self.db.audit("agent" if origin != "user" else "user", "extension.proposed", {"id": sid, "capability": capability})
        return self.db.get("skills", sid)  # type: ignore[return-value]

    @staticmethod
    def _template(name: str, capability: str, deps: list[str]) -> str:
        return textwrap.dedent(f'''\
            """Candidate extension: {capability}

            This is a SKELETON generated as a proposal. The handler deliberately fails until it is implemented, so it can
            never be mistaken for a working capability. Edit it (or ask the agent to draft an implementation), then run the test.
            """
            from nexus.tools.registry import Tool, fail, ok

            DEPENDENCIES = {deps!r}


            def handler(ctx, args):
                return fail("{name} is not implemented yet")


            def register(registry):
                registry.register(Tool(
                    name="{name}", description={capability!r},
                    input_schema={{"type": "object", "properties": {{}}, "additionalProperties": False}},
                    handler=handler, effects={{"read"}}, risk="medium"))


            # Self-test executed in an isolated process before activation. Each case: (args, predicate(result) -> bool)
            TESTS = []
            ''')

    def test_extension(self, sid: str) -> dict:
        sk = self.db.get("skills", sid)
        if not sk or sk["kind"] != "extension":
            raise ValueError("no such extension proposal")
        file = sk["spec"]["file"]
        runner = textwrap.dedent('''
            import importlib.util, json, sys
            sys.path.insert(0, sys.argv[2])
            spec = importlib.util.spec_from_file_location("ext_under_test", sys.argv[1])
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            from nexus.tools.registry import ToolRegistry, ToolContext
            reg = ToolRegistry(); m.register(reg)
            tests = getattr(m, "TESTS", [])
            out = {"tools": [t.name for t in reg.all()], "cases": []}
            for tool_name, args, pred in tests:
                t = reg.get(tool_name)
                try:
                    r = t.handler(ToolContext(app=None), args); out["cases"].append({"tool": tool_name, "passed": bool(pred(r))})
                except Exception as e:
                    out["cases"].append({"tool": tool_name, "passed": False, "error": str(e)})
            print(json.dumps(out))
        ''')
        missing = []
        for d in sk["spec"].get("dependencies", []):
            mod = re.split(r"[=<>!~\[]", d)[0].replace("-", "_")
            if subprocess.run([sys.executable, "-c", f"import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('{mod}') else 1)"]).returncode:
                missing.append(d)
        res: dict = {"missing_dependencies": missing}
        backend_root = str(Path(__file__).resolve().parents[2])
        with tempfile.TemporaryDirectory() as td:
            try:
                p = subprocess.run([sys.executable, "-I", "-c", runner, file, backend_root], capture_output=True, text=True, timeout=60, cwd=td)
                res["returncode"] = p.returncode
                res["stderr"] = p.stderr[-800:]
                data = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
                res.update(data)
            except (subprocess.TimeoutExpired, ValueError, IndexError) as e:
                res["error"] = f"self-test could not run: {e}"
        cases = res.get("cases", [])
        res["passed"] = bool(res.get("tools")) and not missing and bool(cases) and all(c["passed"] for c in cases) and res.get("returncode") == 0
        if not cases:
            res["note"] = "The extension defines no TESTS, so it cannot be activated. Add at least one test case."
        self.db.update("skills", sid, {"tests": res, "status": "tested" if res["passed"] else "test_failed", "updated": now()})
        return self.db.get("skills", sid)  # type: ignore[return-value]

    def install_dependencies(self, sid: str, by: str = "user") -> dict:
        """Human-triggered only (the agent has no tool for this). Installs the packages declared in the proposal."""
        if by.lower() in ("agent", "model"):
            raise PermissionError("Only a human can install dependencies.")
        sk = self.db.get("skills", sid)
        if not sk or sk["kind"] != "extension":
            raise ValueError("no such extension proposal")
        pkgs = [d for d in sk["spec"].get("dependencies", []) if PKG_RX.match(d)]
        if not pkgs:
            return {"installed": [], "output": "no dependencies declared"}
        self.db.audit(by, "extension.install_deps", {"id": sid, "packages": pkgs})
        p = subprocess.run([sys.executable, "-m", "pip", "install", *pkgs], capture_output=True, text=True, timeout=900)
        return {"installed": pkgs if p.returncode == 0 else [], "returncode": p.returncode, "output": (p.stdout + p.stderr)[-2000:]}

    def activate_extension(self, sid: str, by: str = "user") -> dict:
        if by.lower() in ("agent", "model", "llm", "system"):
            raise PermissionError("Only a human can activate an extension.")
        sk = self.db.get("skills", sid)
        if not sk or sk["kind"] != "extension":
            raise ValueError("no such extension proposal")
        if sk["status"] != "tested":
            raise ValueError("extension must pass its isolated self-test first")
        src = Path(sk["spec"]["file"])
        dest = self.app.settings.path("extensions", "active", f"{sk['name']}.py")
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        results = self.app.tools.load_plugins(dest.parent)
        self.db.update("skills", sid, {"status": "active", "updated": now()})
        self.db.audit(by, "extension.activate", {"id": sid, "name": sk["name"]})
        return {"id": sid, "loaded": results}

    def deactivate_extension(self, sid: str, by: str = "user") -> None:
        sk = self.db.get("skills", sid)
        if not sk:
            return
        (self.app.settings.path("extensions", "active", f"{sk['name']}.py")).unlink(missing_ok=True)
        for t in list(self.app.tools.all()):
            if t.source == f"plugin:{sk['name']}.py":
                self.app.tools.unregister(t.name)
        self.db.update("skills", sid, {"status": "disabled", "updated": now()})
        self.db.audit(by, "extension.deactivate", {"id": sid})
