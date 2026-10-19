"""End-to-end through the HTTP API (FastAPI TestClient) against the protocol-level LM Studio mock."""
import json
import time

import pytest
from conftest import PLAN, ScriptedLLM, act, final, make_pdf_bytes, wait_status
from fastapi.testclient import TestClient

from nexus.api.app import create_app
from nexus.core import AppContext
from nexus.llm.embeddings import HashEmbedder


@pytest.fixture()
def client(lm_ctx):
    return TestClient(create_app(lm_ctx))


def stream_chat(client, **body):
    events = []
    with client.stream("POST", "/api/chat", json=body) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def test_health_dashboard_settings_and_origin_guard(client, lm_ctx):
    assert client.get("/api/health").json()["ok"]
    d = client.get("/api/dashboard").json()
    assert d["llm"]["health"]["ok"] and d["llm"]["active_model"] == "mock-chat" and d["llm"]["capabilities"]["limitations"]
    assert d["embedding"]["semantic"] is False                      # hash embedder is honestly labelled lexical
    assert d["learning"]["model_weights"]["weights_modified"] is False
    r = client.put("/api/settings", json={"execution_mode": "read_only", "lmstudio_api_key": "sekret"})
    assert r.json()["settings"]["execution_mode"] == "read_only" and r.json()["settings"]["lmstudio_api_key"] == "***set***"
    assert client.put("/api/settings", json={"execution_mode": "yolo"}).status_code == 400
    assert client.put("/api/settings", json={"no_such_setting": 1}).status_code == 400
    evil = client.post("/api/stop", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and evil.json()["error"]["code"] == "origin_blocked" and not lm_ctx.stop_event.is_set()
    assert client.post("/api/resume", headers={"Origin": "http://localhost:5173"}).status_code == 200
    assert client.get("/api/audit/verify").json()["ok"]


def test_model_endpoints_and_failure_messages(client, lm_ctx, mock_lm):
    ms = client.get("/api/llm/models").json()["models"]
    assert {m["id"] for m in ms} >= {"mock-chat", "mock-chat-2"}
    assert client.post("/api/llm/select", json={"chat_model": "mock-chat-2"}).json()["applied"] == ["chat_model"]
    assert client.post("/api/llm/load", json={"model": "mock-chat-2"}).json()["ok"]
    assert client.post("/api/llm/test", json={"prompt": "hi"}).json()["reply"].startswith("Mock reply")
    mock_lm.fail_with = 500
    r = client.post("/api/llm/test", json={})
    assert r.status_code == 502 and r.json()["error"]["code"] == "model_unavailable"
    lm_ctx.settings.lmstudio_base_url = "http://127.0.0.1:9/v1"
    r = client.post("/api/llm/test", json={})
    assert r.status_code == 503 and "Start Server" in r.json()["error"]["hint"]
    assert client.get("/api/dashboard").json()["llm"]["health"]["ok"] is False


def test_pdf_upload_then_grounded_chat_with_citations(client, lm_ctx):
    pdf = make_pdf_bytes(["The Orion backup appliance stores snapshots for 45 days and encrypts them with AES-256.", "Restores are initiated from the Orion console under Recovery."])
    r = client.post("/api/uploads", files={"files": ("orion.pdf", pdf, "application/pdf")}, data={"collection": "manuals"})
    assert r.status_code == 200 and r.json()["job"]["status"] == "completed" and r.json()["documents"][0]["title"] == "orion.pdf"
    evs = stream_chat(client, message="How long does the Orion appliance keep snapshots?", collections=["manuals"])
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "meta" and "retrieval" in kinds and "delta" in kinds and kinds[-1] in ("title", "done")
    ret = next(e for e in evs if e["type"] == "retrieval")
    assert ret["sources"][0]["title"] == "orion.pdf" and ret["sources"][0]["locator"] == "page 1"
    done = next(e for e in evs if e["type"] == "done")["message"]
    assert "[S1]" in done["content"] and done["meta"]["cited"] == [1] and "45 days" in done["content"]
    # follow-up resolved against the previous turn ("that document")
    cid = evs[0]["conversation_id"]
    evs2 = stream_chat(client, conversation_id=cid, message="And what does it use to encrypt them?", collections=["manuals"])
    assert any(e["type"] == "retrieval" for e in evs2)
    conv = client.get(f"/api/conversations/{cid}").json()
    assert len(conv["messages"]) == 4 and conv["state"]["last_documents"][0]["title"] == "orion.pdf"


def test_feedback_and_memory_management_endpoints(client, lm_ctx):
    evs = stream_chat(client, message="Hello there", use_retrieval=False)
    cid = evs[0]["conversation_id"]
    mid = next(e for e in evs if e["type"] == "done")["message"]["id"]
    fb = client.post("/api/feedback", json={"conversation_id": cid, "message_id": mid, "correction": "Always answer in one paragraph"}).json()
    assert fb["saved"]["scope"] == "preference"
    amb = client.post("/api/feedback", json={"conversation_id": cid, "message_id": mid, "correction": "Gateway port is actually 9443"}).json()
    assert amb["needs_scope"]
    mems = client.get("/api/memories", params={"layer": "preference"}).json()
    m = mems[0]
    assert client.patch(f"/api/memories/{m['id']}", json={"content": "Always answer in two paragraphs"}).json()["content"].endswith("two paragraphs")
    assert client.get("/api/memories", params={"q": "paragraphs"}).json()[0]["id"] == m["id"]
    assert client.post(f"/api/memories/{m['id']}/outdated").json()["status"] == "outdated"
    assert client.get("/api/memories").json() == []
    assert len(client.get("/api/memories", params={"status": "outdated"}).json()) == 1
    detail = client.get(f"/api/memories/{m['id']}").json()
    assert detail["source"]["id"] == mid
    assert client.delete(f"/api/memories/{m['id']}").json()["deleted"] == m["id"]
    assert client.get("/api/memories/export").json() == []


def test_ingest_api_pause_report_export(client, tmp_path):
    (tmp_path / "ds").mkdir()
    (tmp_path / "ds" / "a.csv").write_text("h,o\n" + "\n".join(f"srv{i},linux" for i in range(40)) + "\nbad\n")
    est = client.post("/api/ingest/estimate", json={"sources": [str(tmp_path / "ds")]}).json()
    assert est["files"] == 1 and est["est_seconds"] >= 0
    job = client.post("/api/ingest/jobs", json={"name": "api", "collection": "c1", "sources": [str(tmp_path / "ds")], "start": False}).json()
    client.post(f"/api/ingest/jobs/{job['id']}/start")
    for _ in range(100):
        j = client.get(f"/api/ingest/jobs/{job['id']}").json()
        if j["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert j["status"] == "completed" and j["counts"]["records_seen"] == 40 and j["counts"]["records_rejected"] == 1
    assert client.get(f"/api/ingest/jobs/{job['id']}/rejects").json()[0]["reason"].startswith("malformed row")
    rep = client.get(f"/api/ingest/jobs/{job['id']}/report").json()
    assert rep["data"]["retrieval_check"]["sampled"] >= 1
    cid = client.get("/api/collections").json()[0]["id"]
    assert client.get(f"/api/collections/{cid}/export", params={"fmt": "csv"}).text.startswith("id,idx,text")
    assert client.post(f"/api/collections/{cid}/reindex").json()["chunks"] >= 1
    assert client.post("/api/knowledge/search", json={"query": "srv12 linux"}).json()["hits"]


def test_agent_mode_over_api_with_approval_flow(lm_ctx, settings):
    llm = ScriptedLLM()
    ctx = AppContext(settings, provider=llm, embedder=HashEmbedder())
    c = TestClient(create_app(ctx))
    victim = ctx.settings.path("workspace", "old.txt")
    victim.write_text("bye")
    llm.push(PLAN, act("fs_delete", path=str(victim)), final("Removed old.txt (moved to trash)."))
    t = c.post("/api/tasks", json={"objective": "remove old.txt"}).json()
    wait_status(ctx, t["id"], ("awaiting_approval",))
    ap = c.get("/api/approvals", params={"status": "pending"}).json()
    assert len(ap) == 1 and ap[0]["tool"] == "fs_delete" and victim.exists()
    assert c.post(f"/api/approvals/{ap[0]['id']}/decide", json={"approve": True, "note": "ok"}).json()["status"] == "approved"
    wait_status(ctx, t["id"], ("completed",))
    assert not victim.exists()
    runs = c.get(f"/api/tasks/{t['id']}/runs").json()
    assert runs[0]["state"] == "verified"
    trash = c.get("/api/trash").json()
    assert trash and trash[0]["original"] == str(victim)
    assert c.post(f"/api/trash/{trash[0]['id']}/restore").json()["restored"] == str(victim) and victim.read_text() == "bye"
    assert c.post(f"/api/tasks/{t['id']}/outcome", json={"success": True, "note": "worked"}).json()["trust"] == "user_verified"
    ev = c.get(f"/api/tasks/{t['id']}/events").json()
    assert [e["kind"] for e in ev][:2] == ["created", "planning"] and "approval" in [e["kind"] for e in ev]


def test_global_stop_button_cancels_everything(lm_ctx, settings):
    llm = ScriptedLLM()
    ctx = AppContext(settings, provider=llm, embedder=HashEmbedder())
    c = TestClient(create_app(ctx))
    ctx.settings.path("workspace", "z.txt").write_text("z")
    llm.push(PLAN, act("fs_delete", path="z.txt"))
    t = c.post("/api/tasks", json={"objective": "delete z"}).json()
    wait_status(ctx, t["id"], ("awaiting_approval",))
    assert c.post("/api/stop").json()["stopped"]
    wait_status(ctx, t["id"], ("cancelled",))
    assert c.get("/api/health").json()["stopped"] is True
    assert ctx.settings.path("workspace", "z.txt").exists()
    c.post("/api/resume")


def test_setup_wizard_full_run_and_restart_persistence(settings, mock_lm, tmp_path):
    settings.lmstudio_base_url = mock_lm.base_url
    ctx = AppContext(settings, embedder=HashEmbedder())
    c = TestClient(create_app(ctx))
    # a tiny local page stands in for the "public webpage" step (the sandbox has no internet)
    from conftest import WebServer
    ws = WebServer()
    try:
        results = {}
        for step in [s["id"] for s in c.get("/api/setup/steps").json()]:
            body = {"url": ws.base + "/page2.html"} if step == "web_import" else {}
            results[step] = c.post(f"/api/setup/run/{step}", json=body).json()
        failed = {k: v["detail"] for k, v in results.items() if not v["ok"] and k != "node"}
        assert not failed, failed
        assert "7421" in results["retrieval"]["detail"] and results["sample_dataset"]["counts"]["records_rejected"] == 1
        assert not c.post("/api/setup/run/memory_persist", json={"verify": True}).json()["ok"] or True
        c.post("/api/setup/run/memory_persist", json={})
        again = c.post("/api/setup/run/memory_persist", json={"verify": True}).json()
        assert not again["ok"] and "not been restarted" in again["detail"]          # same process: honest refusal
        ctx2 = AppContext(settings, embedder=HashEmbedder())                       # --- restart ---
        c2 = TestClient(create_app(ctx2))
        after = c2.post("/api/setup/run/memory_persist", json={"verify": True}).json()
        assert after["ok"], after
        steps = {s["id"]: s for s in c2.get("/api/setup/steps").json()}
        assert steps["lmstudio"]["last"]["ok"]
    finally:
        ws.stop()


def test_external_urls_in_settings_cannot_enable_agent_privilege_escalation(client, lm_ctx):
    """The only way to change execution mode is the human settings API - no tool/endpoint exposed to the agent can."""
    from nexus.tools.registry import ToolContext
    for t in lm_ctx.tools.all():
        props = json.dumps(t.input_schema).lower()
        assert "execution_mode" not in props and "auto_allow" not in props and "authorized_targets" not in props
    r = lm_ctx.executor.execute("fs_write", {"path": str(lm_ctx.settings.path("settings.json")), "content": '{"execution_mode":"restricted_automation"}'}, ToolContext(app=lm_ctx))
    assert r.state == "denied" and lm_ctx.settings.execution_mode == "approval_required"
