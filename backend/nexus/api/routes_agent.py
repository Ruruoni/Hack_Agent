from __future__ import annotations

from fastapi import Body, FastAPI

from ..tools.registry import ToolContext


def register(app: FastAPI, ctx) -> None:
    db = ctx.db

    # ---------------------------------------------------------------- tasks
    @app.post("/api/tasks")
    def task_create(body: dict = Body(...)):
        return ctx.tasks.create(body["objective"], body.get("conversation_id"), body.get("project_id", "default"))

    @app.get("/api/tasks")
    def task_list(status: str | None = None, limit: int = 50):
        return ctx.tasks.list(status, limit)

    @app.get("/api/tasks/{tid}")
    def task_get(tid: str):
        t = ctx.tasks.get(tid)
        if not t:
            raise ValueError("no such task")
        st = t.get("state") or {}
        t["state"] = {k: v for k, v in st.items() if k in ("step", "tool_calls", "tainted", "elapsed")}
        return t

    @app.get("/api/tasks/{tid}/events")
    def task_events(tid: str, after: int = 0):
        return ctx.tasks.events(tid, after)

    @app.get("/api/tasks/{tid}/runs")
    def task_runs(tid: str):
        return ctx.tasks.runs(tid)

    @app.post("/api/tasks/{tid}/cancel")
    def task_cancel(tid: str):
        return {"cancelled": ctx.tasks.cancel(tid)}

    @app.post("/api/tasks/{tid}/resume")
    def task_resume(tid: str):
        return ctx.tasks.resume(tid)

    @app.post("/api/tasks/{tid}/answer")
    def task_answer(tid: str, body: dict = Body(...)):
        return ctx.tasks.answer(tid, body["answer"])

    @app.post("/api/tasks/{tid}/outcome")
    def task_outcome(tid: str, body: dict = Body(...)):
        return ctx.learning.record_outcome(tid, bool(body["success"]), body.get("note", ""))

    # ---------------------------------------------------------------- approvals (human only)
    @app.get("/api/approvals")
    def approvals(status: str | None = None, limit: int = 100):
        if status == "pending":
            return ctx.perms.pending()
        return ctx.perms.history(limit)

    @app.post("/api/approvals/{aid}/decide")
    def approval_decide(aid: str, body: dict = Body(...)):
        return ctx.perms.decide_approval(aid, bool(body["approve"]), "user", body.get("note", ""))

    # ---------------------------------------------------------------- tools
    @app.get("/api/tools")
    def tools():
        return [t.describe() for t in ctx.tools.all()]

    @app.get("/api/tools/gaps")
    def gaps():
        return {"missing_dependencies": [{"tool": t.name, "missing": t.missing_dependencies()} for t in ctx.tools.all() if t.missing_dependencies()],
                "proposals": db.query("SELECT id,name,description,status,created FROM skills WHERE kind='extension' ORDER BY created DESC")}

    @app.get("/api/tools/commands")
    def commands():
        from ..tools.builtin.execution import _commands
        return {k: {"description": v["desc"], "effects": v["effects"], "risk": v["risk"], "params": list(v.get("params", {})), "external": v.get("external", False)}
                for k, v in _commands(ctx).items()}

    # ---------------------------------------------------------------- skills & extensions
    @app.get("/api/skills")
    def skills(kind: str | None = None, status: str | None = None):
        return ctx.skills.list(kind, status)

    @app.post("/api/skills")
    def skill_create(body: dict = Body(...)):
        return ctx.skills.create_candidate(body["spec"], "user", body.get("description", ""))

    @app.get("/api/skills/versions/{name}")
    def skill_versions(name: str):
        return ctx.skills.versions(name)

    @app.post("/api/skills/{sid}/evaluate")
    def skill_eval(sid: str):
        return ctx.skills.evaluate(sid)

    @app.post("/api/skills/{sid}/approve")
    def skill_approve(sid: str):
        return ctx.skills.approve(sid, "user")

    @app.post("/api/skills/{sid}/disable")
    def skill_disable(sid: str):
        ctx.skills.set_status(sid, "disabled", "user")
        return ctx.skills.get(sid)

    @app.post("/api/skills/{sid}/run")
    def skill_run(sid: str, body: dict = Body(default={})):
        return ctx.skills.run(sid, body.get("params") or {}, ToolContext(app=ctx))

    @app.post("/api/extensions/propose")
    def ext_propose(body: dict = Body(...)):
        return ctx.skills.propose_extension(body["capability"], body.get("rationale", ""), "user", body.get("code"), body.get("dependencies"))

    @app.post("/api/extensions/{sid}/test")
    def ext_test(sid: str):
        return ctx.skills.test_extension(sid)

    @app.post("/api/extensions/{sid}/install-deps")
    def ext_deps(sid: str):
        return ctx.skills.install_dependencies(sid, "user")

    @app.post("/api/extensions/{sid}/activate")
    def ext_activate(sid: str):
        return ctx.skills.activate_extension(sid, "user")

    @app.post("/api/extensions/{sid}/deactivate")
    def ext_deactivate(sid: str):
        ctx.skills.deactivate_extension(sid, "user")
        return {"deactivated": sid}

    # ---------------------------------------------------------------- evaluation & learning
    @app.get("/api/eval/cases")
    def eval_cases():
        return ctx.evaluation.list_cases()

    @app.post("/api/eval/cases")
    def eval_add(body: dict = Body(...)):
        return ctx.evaluation.add_case(body["name"], body["kind"], body["spec"], "user")

    @app.delete("/api/eval/cases/{cid}")
    def eval_del(cid: str):
        ctx.evaluation.delete_case(cid)
        return {"deleted": cid}

    @app.post("/api/eval/run")
    def eval_run():
        return ctx.evaluation.run_suite("manual")

    @app.get("/api/eval/runs")
    def eval_runs():
        return ctx.evaluation.runs()

    @app.get("/api/eval/runs/{rid}")
    def eval_run_get(rid: str):
        return db.get("eval_runs", rid)

    @app.get("/api/learning/overview")
    def learning_overview():
        return ctx.learning.overview()

    # ---------------------------------------------------------------- training (optional, separate system)
    t = ctx.training

    @app.get("/api/training/hardware")
    def tr_hw():
        return t.hardware()

    @app.post("/api/training/check-model")
    def tr_check(body: dict = Body(...)):
        return t.check_model(body["ref"])

    @app.post("/api/training/datasets")
    def tr_prepare(body: dict = Body(...)):
        return t.prepare_dataset(body["source"], body.get("license", ""), bool(body.get("permitted_use_confirmed")), body.get("name", ""), int(body.get("min_examples", 20)))

    @app.get("/api/training/datasets")
    def tr_datasets():
        return t.datasets()

    @app.post("/api/training/plan")
    def tr_plan(body: dict = Body(...)):
        return t.plan(body["base_model"], body["dataset_id"], body.get("method", "lora"), body.get("params"))

    @app.get("/api/training/runs")
    def tr_runs():
        return t.list()

    @app.get("/api/training/runs/{rid}")
    def tr_get(rid: str):
        r = t.get(rid)
        if not r:
            raise ValueError("no such run")
        return {**r, "log_tail": t.log_tail(rid, 30)}

    @app.post("/api/training/runs/{rid}/request-approval")
    def tr_req(rid: str):
        return {"approval_id": t.request_approval(rid)}

    @app.post("/api/training/runs/{rid}/start")
    def tr_start(rid: str):
        return t.start(rid)

    @app.post("/api/training/runs/{rid}/stop")
    def tr_stop(rid: str):
        t.stop(rid)
        return {"stopping": True}

    @app.post("/api/training/runs/{rid}/decide")
    def tr_decide(rid: str, body: dict = Body(...)):
        return t.decide(rid, bool(body["accept"]), "user", body.get("note", ""))

    @app.get("/api/training/runs/{rid}/export-plan")
    def tr_export(rid: str, quant: str = "Q4_K_M"):
        return t.export_plan(rid, quant)

    @app.get("/api/training/registry")
    def tr_registry():
        return t.registry()

    @app.post("/api/training/registry/{mid}/activate")
    def tr_activate(mid: str, body: dict = Body(...)):
        return t.activate(mid, body["lmstudio_model_id"], "user")

    @app.post("/api/training/registry/{mid}/rollback")
    def tr_rollback(mid: str):
        return t.rollback(mid, "user")
