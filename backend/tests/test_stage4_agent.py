"""Stage 4: tools, permissions, approvals, orchestration, cancellation, injection defence."""
import json
import os
import threading
import time

import pytest
from conftest import PLAN, ScriptedLLM, act, final, wait_status, write

from nexus.llm.base import ChatResult, ToolCall
from nexus.permissions.policy import PermissionDenied
from nexus.tools.registry import ToolContext


def run_task(ctx, objective, *replies, timeout=20):
    ctx.llm.push(PLAN, *replies)
    t = ctx.tasks.create(objective)
    return wait_status(ctx, t["id"], ("completed", "failed", "cancelled", "awaiting_input"), timeout)


def test_permitted_local_operation_is_executed_and_verified(agent_ctx):
    ws = agent_ctx.settings.path("workspace")
    t = run_task(agent_ctx, "write a note", act("fs_write", path="note.txt", content="hello nexus"), final("I wrote note.txt."))
    assert t["status"] == "completed"
    assert (ws / "note.txt").read_text() == "hello nexus"
    run = agent_ctx.tasks.runs(t["id"])[0]
    assert run["state"] == "verified" and run["verified"] == 1 and "SHA-256" in run["verification"]
    assert "independently verified: 1" in t["result"]


def test_destructive_action_requires_approval_then_runs_after_user_approves(agent_ctx):
    ws = agent_ctx.settings.path("workspace")
    victim = write(ws, "victim.txt", "precious")
    agent_ctx.llm.push(PLAN, act("fs_delete", path=str(victim)), final("Deleted it."))
    t = agent_ctx.tasks.create("delete victim.txt")
    wait_status(agent_ctx, t["id"], ("awaiting_approval",))
    assert victim.exists()                                          # NOT executed while waiting
    appr = agent_ctx.perms.pending()[0]
    assert appr["tool"] == "fs_delete" and appr["risk"] == "high"
    with pytest.raises(PermissionDenied):
        agent_ctx.perms.decide_approval(appr["id"], True, decided_by="agent")   # the model cannot approve itself
    agent_ctx.perms.decide_approval(appr["id"], True, decided_by="user")
    t = wait_status(agent_ctx, t["id"], ("completed",))
    assert not victim.exists()
    trash = list(agent_ctx.settings.path("trash").glob("*/victim.txt"))
    assert trash, "deleted files must be recoverable"
    run = agent_ctx.tasks.runs(t["id"])[0]
    assert run["verified"] == 1
    assert agent_ctx.perms.history()[0]["status"] == "used"


def test_denied_approval_means_nothing_happens(agent_ctx):
    ws = agent_ctx.settings.path("workspace")
    victim = write(ws, "keep.txt", "keep me")
    agent_ctx.llm.push(PLAN, act("fs_delete", path=str(victim)), final("The deletion was declined, nothing changed."))
    t = agent_ctx.tasks.create("delete keep.txt")
    wait_status(agent_ctx, t["id"], ("awaiting_approval",))
    agent_ctx.perms.decide_approval(agent_ctx.perms.pending()[0]["id"], False, decided_by="user", note="no")
    t = wait_status(agent_ctx, t["id"], ("completed",))
    assert victim.exists()
    assert agent_ctx.tasks.runs(t["id"])[0]["state"] == "approval_denied"


def test_approval_is_bound_to_exact_arguments_and_single_use(agent_ctx):
    p = agent_ctx.perms
    aid = p.request_approval(None, "fs_delete", {"path": "/a"}, "high", "test")
    p.decide_approval(aid, True)
    assert not p.consume(aid, "fs_delete", {"path": "/OTHER"})      # different args -> refused
    assert p.consume(aid, "fs_delete", {"path": "/a"})
    assert not p.consume(aid, "fs_delete", {"path": "/a"})           # single use


def test_cancel_running_task_while_waiting(agent_ctx):
    ws = agent_ctx.settings.path("workspace")
    victim = write(ws, "x.txt", "x")
    agent_ctx.llm.push(PLAN, act("fs_delete", path=str(victim)))
    t = agent_ctx.tasks.create("delete x")
    wait_status(agent_ctx, t["id"], ("awaiting_approval",))
    assert agent_ctx.tasks.cancel(t["id"])
    t = wait_status(agent_ctx, t["id"], ("cancelled",))
    assert victim.exists()
    assert agent_ctx.db.one("SELECT status FROM approvals")["status"] == "cancelled"


def test_cancel_kills_running_sandbox_and_global_stop(agent_ctx):
    agent_ctx.settings.execution_mode = "restricted_automation"
    agent_ctx.settings.auto_allow_tools = ["python_sandbox"]
    agent_ctx.llm.push(PLAN, act("python_sandbox", code="while True:\n    pass\n", timeout=30), final("never reached"))
    t = agent_ctx.tasks.create("spin forever")
    wait_status(agent_ctx, t["id"], ("running",))
    time.sleep(1.0)
    t0 = time.time()
    agent_ctx.global_stop()                                          # the big red button
    t = wait_status(agent_ctx, t["id"], ("cancelled",), 10)
    assert time.time() - t0 < 5
    agent_ctx.clear_stop()


def test_read_only_mode_denies_writes_but_allows_reads(agent_ctx):
    agent_ctx.settings.execution_mode = "read_only"
    ws = agent_ctx.settings.path("workspace")
    write(ws, "r.txt", "readable")
    t = run_task(agent_ctx, "inspect", act("fs_write", path="w.txt", content="x"), act("fs_read", path="r.txt"), final("Write was denied; read worked."))
    states = [r["state"] for r in agent_ctx.tasks.runs(t["id"])]
    assert states == ["denied", "verified" if False else "completed"]
    assert not (ws / "w.txt").exists()


def test_restricted_automation_auto_allows_only_listed_low_risk_tools(agent_ctx):
    agent_ctx.settings.execution_mode = "restricted_automation"
    ws = agent_ctx.settings.path("workspace")
    write(ws, "d.csv", "a,b\n1,2\n3,4\n")
    agent_ctx.llm.push(PLAN, act("csv_inspect", path="d.csv"), act("fs_delete", path="d.csv"))
    t = agent_ctx.tasks.create("inspect then delete")
    wait_status(agent_ctx, t["id"], ("awaiting_approval",))          # csv_inspect ran automatically; delete needs a human
    runs = agent_ctx.tasks.runs(t["id"])
    assert runs[0]["tool"] == "csv_inspect" and runs[0]["state"] in ("completed", "verified")
    agent_ctx.tasks.cancel(t["id"])


def test_invalid_tool_and_arguments_are_rejected_and_model_can_recover(agent_ctx):
    t = run_task(agent_ctx, "bad calls", act("does_not_exist"), act("fs_read"), act("fs_list"), final("Recovered after two invalid calls."))
    states = [r["state"] for r in agent_ctx.tasks.runs(t["id"])]
    assert states[:2] == ["invalid", "invalid"] and t["status"] == "completed"


def test_malformed_model_output_is_repaired_then_fails_cleanly(agent_ctx):
    agent_ctx.llm.push(PLAN, "I think I should list files", "still not json", "nope")
    t = agent_ctx.tasks.create("garbage model")
    t = wait_status(agent_ctx, t["id"], ("failed",))
    assert "could not produce a valid action" in t["error"]
    agent_ctx.llm.replies.clear()
    agent_ctx.llm.push('Sure!\n```json\n{"action":"final","answer":"ok from fenced json"}\n```')
    agent_ctx.llm.push(PLAN)       # consumed first by planning... order: plan then action
    agent_ctx.llm.replies.reverse()
    t2 = agent_ctx.tasks.create("fenced")
    assert wait_status(agent_ctx, t2["id"], ("completed", "failed"))["status"] == "completed"


def test_step_limit_prevents_infinite_loops(agent_ctx):
    agent_ctx.settings.max_task_steps = 4
    ws = agent_ctx.settings.path("workspace")
    write(ws, "a.txt", "a")
    agent_ctx.llm.push(PLAN, *[act("fs_list", path=".", pattern=f"*{i}") for i in range(10)])
    t = agent_ctx.tasks.create("loop")
    t = wait_status(agent_ctx, t["id"], ("failed",))
    assert "Step limit" in t["error"] and len(agent_ctx.tasks.runs(t["id"])) <= 4


def test_repeated_failing_call_is_blocked(agent_ctx):
    agent_ctx.settings.max_step_retries = 1
    agent_ctx.llm.push(PLAN, *[act("fs_read", path="missing.txt")] * 4, final("Gave up: file does not exist."))
    t = agent_ctx.tasks.create("read missing")
    t = wait_status(agent_ctx, t["id"], ("completed",))
    hist = agent_ctx.tasks.get(t["id"])["state"]["history"]
    assert any("will not be retried" in h["content"] for h in hist)


def test_clarifying_question_pauses_then_resumes(agent_ctx):
    agent_ctx.llm.push('{"clarifying_questions":["Which file should I analyse?"],"steps":[],"success_criteria":""}')
    t = agent_ctx.tasks.create("analyse the file")
    t = wait_status(agent_ctx, t["id"], ("awaiting_input",))
    assert "Which file" in t["result"]
    agent_ctx.llm.push(final("Analysed it."))
    agent_ctx.tasks.answer(t["id"], "report.csv")
    t = wait_status(agent_ctx, t["id"], ("completed",))
    assert any("report.csv" in h["content"] for h in t["state"]["history"])


def test_native_tool_calling_path(agent_ctx):
    agent_ctx.llm.native = True
    ws = agent_ctx.settings.path("workspace")
    write(ws, "n.txt", "native")
    agent_ctx.llm.push(PLAN, ChatResult(tool_calls=[ToolCall("1", "fs_read", {"path": "n.txt"})]), ChatResult(text="The file says native."))
    t = agent_ctx.tasks.create("native")
    t = wait_status(agent_ctx, t["id"], ("completed",))
    assert agent_ctx.tasks.runs(t["id"])[0]["tool"] == "fs_read" and "native" in t["result"]
    assert agent_ctx.llm.calls[-1]["tools"]                          # tool schemas were actually sent


def test_task_recovery_after_restart(agent_ctx, settings):
    from nexus.core import AppContext
    from nexus.llm.embeddings import HashEmbedder
    agent_ctx.db.insert("tasks", {"id": "task_zombie", "objective": "x", "status": "running", "plan": {}, "state": {"history": [], "step": 2, "tool_calls": 1},
                                  "created": time.time(), "updated": time.time()})
    llm = ScriptedLLM()
    c2 = AppContext(settings, provider=llm, embedder=HashEmbedder())
    z = c2.tasks.get("task_zombie")
    assert z["status"] == "failed" and "restart" in z["error"]
    llm.push(PLAN, final("Resumed and finished."))
    c2.tasks.resume("task_zombie")
    wait_status_ctx = wait_status(c2, "task_zombie", ("completed",))
    assert wait_status_ctx["state"]["step"] >= 3                     # continued from the checkpoint, not from zero


# ---------------------------------------------------------------- path & command safety
def test_path_policy_blocks_traversal_symlinks_and_protected_files(agent_ctx):
    pp = agent_ctx.perms.paths
    ws = agent_ctx.settings.path("workspace")
    for bad in ["../nexus.db", "/etc/passwd", str(agent_ctx.settings.db_path), "../settings.json", str(agent_ctx.settings.path("logs", "x"))]:
        with pytest.raises(PermissionDenied):
            pp.resolve(bad)
    os.symlink("/etc", ws / "link")
    with pytest.raises(PermissionDenied):
        pp.resolve("link/passwd")
    assert pp.resolve("ok.txt") == (ws / "ok.txt").resolve()


def test_agent_cannot_reach_settings_or_approvals_through_tools(agent_ctx):
    names = {t.name for t in agent_ctx.tools.all()}
    assert not any(w in n for n in names for w in ("setting", "permission", "approv", "update_config", "set_mode"))
    res = agent_ctx.executor.execute("fs_read", {"path": str(agent_ctx.settings.path("settings.json"))}, ToolContext(app=agent_ctx))
    assert res.state == "denied"
    res = agent_ctx.executor.execute("db_query", {"sql": "SELECT * FROM approvals"}, ToolContext(app=agent_ctx))
    assert not res.ok                                                 # approvals table is not exposed to SQL tool
    res = agent_ctx.executor.execute("db_query", {"sql": "DELETE FROM memories"}, ToolContext(app=agent_ctx))
    assert not res.ok


def test_sandbox_runs_safe_code_and_blocks_escapes(agent_ctx):
    ex = lambda code, **kw: agent_ctx.executor.execute("python_sandbox", {"code": code, **kw}, ToolContext(app=agent_ctx))  # noqa: E731
    agent_ctx.settings.execution_mode = "restricted_automation"
    agent_ctx.settings.auto_allow_tools = ["python_sandbox"]
    r = ex("import statistics\nRESULT = statistics.mean(DATA)\nprint('hi')", data=[1, 2, 3, 6])
    assert r.ok and r.data["result"] == 3 and r.data["stdout"].strip() == "hi"
    for evil in ["import os\nos.system('id')", "open('/etc/passwd').read()", "().__class__.__bases__[0].__subclasses__()",
                 "import subprocess", "__import__('os')", "eval('1+1')", "getattr(int,'x')", "import socket", "from pathlib import Path"]:
        out = ex(evil)
        assert not out.ok and ("rejected" in out.data["error"] or "not allowed" in out.data["error"]), (evil, out.data)
    t0 = time.time()
    out = ex("while True:\n  pass", timeout=2)
    assert not out.ok and time.time() - t0 < 10
    mem = ex("x = bytearray(2*1024*1024*1024)\nRESULT=len(x)")
    assert not mem.ok


def test_sandbox_needs_approval_by_default(agent_ctx):
    d = agent_ctx.perms.decide("python_sandbox", {"exec_sandbox"}, "high", {})
    assert d.outcome == "approval"


def test_commands_are_allowlisted_validated_and_scoped(agent_ctx):
    ex = lambda **kw: agent_ctx.executor.execute("run_command", kw, ToolContext(app=agent_ctx))  # noqa: E731
    agent_ctx.settings.execution_mode = "restricted_automation"
    agent_ctx.settings.auto_allow_tools = ["run_command"]
    r = ex(command="python_version")
    assert r.ok and "Python" in (r.data["stdout"] + r.data["stderr"])
    assert ex(command="rm", params={}).state == "denied"                                     # not registered
    assert ex(command="ping", params={"host": "10.1.2.3", "count": "2"}).state == "denied"   # outside authorized scope
    assert "authorized targets" in ex(command="ping", params={"host": "10.1.2.3", "count": "2"}).data["error"]
    agent_ctx.settings.authorized_targets = ["192.168.50.0/24", "lab.example"]
    assert agent_ctx.perms.target_allowed("192.168.50.7") and not agent_ctx.perms.target_allowed("192.168.51.7")
    assert ex(command="ping", params={"host": "-c1 evil", "count": "2"}).state == "denied"   # argument injection
    assert ex(command="ping", params={"host": "lab.example; whoami", "count": "2"}).state == "denied"
    assert ex(command="python_version", params={"x": "1"}).state == "denied"                 # unexpected param
    # ping to an authorized target still needs a human even in restricted automation (network_write effect)
    d = agent_ctx.perms.decide("run_command", {"network_write"}, "medium", {}, targets=["192.168.50.7"])
    assert d.outcome == "approval"


# ---------------------------------------------------------------- injection
def test_malicious_web_page_is_flagged_and_treated_as_data(agent_ctx, web_server):
    agent_ctx.settings.execution_mode = "restricted_automation"
    agent_ctx.settings.auto_allow_tools = ["web_fetch", "fs_write"]
    ws = agent_ctx.settings.path("workspace")
    agent_ctx.llm.push(PLAN, act("web_fetch", url=web_server.base + "/evil.html"),
                       act("fs_write", path="after.txt", content="written after reading hostile page"),
                       final("The page contained embedded instructions which I ignored."))
    t = agent_ctx.tasks.create("summarise evil page")
    t = wait_status(agent_ctx, t["id"], ("awaiting_approval",))      # tainted task -> even a normally auto-allowed write needs a human
    evs = agent_ctx.tasks.events(t["id"])
    assert any(e["kind"] == "injection" for e in evs)
    obs = [h["content"] for h in agent_ctx.tasks.get(t["id"])["state"]["history"] if h["content"].startswith("OBSERVATION [web_fetch]")][0]
    assert "<untrusted_tool_output" in obs and "SECURITY WARNING" in obs
    assert not (ws / "after.txt").exists()
    assert agent_ctx.db.scalar("SELECT COUNT(*) FROM audit_log WHERE event='injection.detected'") >= 1
    agent_ctx.tasks.cancel(t["id"])


# ---------------------------------------------------------------- tools
def test_analysis_tools(agent_ctx, tmp_path):
    ws = agent_ctx.settings.path("workspace")
    ex = lambda tool, **a: agent_ctx.executor.execute(tool, a, ToolContext(app=agent_ctx))  # noqa: E731
    write(ws, "sales.csv", "region,amount\n" + "\n".join(f"{r},{i}" for i, r in enumerate(["eu", "us", "eu", "apac"] * 50)))
    r = ex("csv_inspect", path="sales.csv")
    assert r.ok and r.data["rows"] == 200 and r.data["columns"] == ["region", "amount"]
    r = ex("data_analyze", path="sales.csv", operation="value_counts", column="region")
    assert r.data["top"][0]["count"] == 100
    r = ex("data_analyze", path="sales.csv", operation="groupby", column="amount", group_by="region", agg="sum")
    assert r.ok and r.data["total_groups"] == 3
    r = ex("data_analyze", path="sales.csv", operation="describe", column="amount")
    assert abs(r.data["mean"] - 99.5) < 1e-6
    write(ws, "svc.py", "import subprocess\nPASSWORD = 'hunter2hunter2'\ndef run(c):\n    subprocess.run(c, shell=True)  # TODO fix\n")
    r = ex("code_analyze", path="svc.py")
    issues = " ".join(f["issue"] for f in r.data["findings"])
    assert "shell=True" in issues and "hard-coded secret" in issues and "hunter2" not in json.dumps(r.data)
    write(ws, "app.yaml", "server:\n  host: 0.0.0.0\n  debug: true\ndb:\n  password: s3cretpass\n  verify_ssl: false\n")
    r = ex("config_analyze", path="app.yaml")
    assert {"listens on all interfaces", "debug mode enabled", "TLS/SSL verification disabled"} <= {f["issue"] for f in r.data["findings"]}
    write(ws, "x.log", "2024-05-01 10:00:01 INFO start\n" + "".join(f"2024-05-01 10:01:{i:02d} ERROR db timeout after {i}s id={i}\n" for i in range(30)) + "2024-05-01 10:02:00 WARN slow\n")
    r = ex("log_analyze", path="x.log")
    assert r.data["levels"]["ERROR"] == 30 and r.data["top_signatures"][0]["count"] == 30 and r.data["error_burst"]["errors"] == 30
    r = ex("json_query", path=str(write(ws, "j.json", '{"a":{"b":[10,20,{"c":"deep"}]}}')), query="a.b[2].c")
    assert r.data["value"] == "deep"
    r = ex("report_generate", title="T", format="csv", rows=[{"a": 1, "b": 2}], filename="out")
    assert r.verified
    r = ex("db_query", sql="SELECT COUNT(*) AS n FROM tool_runs")
    assert r.ok


def test_pcap_summary(agent_ctx):
    import socket
    import struct
    ws = agent_ctx.settings.path("workspace")

    def frame(src, dst, dport, flags):
        eth = b"\xaa" * 6 + b"\xbb" * 6 + b"\x08\x00"
        tcp = struct.pack(">HHIIBBHHH", 40000, dport, 0, 0, 5 << 4, flags, 1000, 0, 0)
        ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0, socket.inet_aton(src), socket.inet_aton(dst))
        return eth + ip + tcp
    pkts = [frame("10.0.0.5", "10.0.0.9", 22 + i % 200, 0x02) for i in range(120)] + [frame("10.0.0.9", "10.0.0.5", 40000, 0x12)]
    data = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
    for i, p in enumerate(pkts):
        data += struct.pack("<IIII", 1700000000 + i, 0, len(p), len(p)) + p
    (ws / "scan.pcap").write_bytes(data)
    r = agent_ctx.executor.execute("pcap_summary", {"path": "scan.pcap"}, ToolContext(app=agent_ctx))
    assert r.ok and r.data["packets"] == 121 and r.data["protocols"]["TCP"] == 121
    assert r.data["hypotheses"] and "SYN" in r.data["hypotheses"][0]["hypothesis"] and r.data["hypotheses"][0]["confidence"] == "low"
    (ws / "bad.pcap").write_bytes(b"not a capture")
    assert not agent_ctx.executor.execute("pcap_summary", {"path": "bad.pcap"}, ToolContext(app=agent_ctx)).ok


def test_missing_dependency_is_reported_not_crashed(agent_ctx):
    from nexus.tools.registry import Tool
    agent_ctx.tools.register(Tool("needs_foo", "x", {"type": "object"}, lambda c, a: {"ok": True}, dependencies=["no_such_module_xyz", "exe:no_such_exe_xyz"]))
    r = agent_ctx.executor.execute("needs_foo", {}, ToolContext(app=agent_ctx))
    assert r.state == "invalid" and "missing dependencies" in r.data["error"]
    assert "needs_foo" not in [t.name for t in agent_ctx.tools.available()]


def test_unknown_execution_mode_fails_safe(agent_ctx):
    agent_ctx.settings.execution_mode = "yolo"          # e.g. a typo in .env must not become 'allow everything'
    d = agent_ctx.perms.decide("fs_delete", {"delete"}, "high", {})
    assert d.outcome == "approval"
    from nexus.config import load_settings
    import os
    os.environ["NEXUS_EXECUTION_MODE"] = "restricted-automation"       # wrong spelling
    try:
        assert load_settings(str(agent_ctx.settings.path("..", "d2"))).execution_mode == "approval_required"
    finally:
        del os.environ["NEXUS_EXECUTION_MODE"]
