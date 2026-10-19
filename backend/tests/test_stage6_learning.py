"""Stage 6: feedback, corrections, utility learning, workflow candidates, regression suite."""
import pytest
from conftest import PLAN, act, final, wait_status, write

from nexus.agent.chat import ChatService
from nexus.core import AppContext
from nexus.llm.embeddings import HashEmbedder
from nexus.llm.lmstudio import LMStudioProvider
from nexus.memory.learning import infer_scope


def _chat_with_answer(ctx, mock_lm, question="How do I measure disk usage?", answer="Use df -h"):
    chat = ChatService(ctx)
    conv = chat.create()
    mock_lm.replies.append(answer)
    evs = list(chat.send(conv["id"], question, use_retrieval=False))
    msg = [e for e in evs if e["type"] == "done"][0]["message"]
    return chat, conv, msg


def test_scope_inference_asks_when_ambiguous():
    assert infer_scope("Always answer in metric units") == ("preference", True)
    assert infer_scope("Just this once, use miles") == ("one_time", True)
    assert infer_scope("The gateway port is actually 9443")[1] is False


def test_correction_becomes_preference_and_is_applied_later(lm_ctx, mock_lm):
    chat, conv, msg = _chat_with_answer(lm_ctx, mock_lm)
    res = lm_ctx.learning.submit_feedback(conv["id"], msg["id"], rating=-1, correction="Always show disk sizes in GiB, never GB")
    assert res["saved"]["scope"] == "preference" and res["saved"]["trust"] == "user_verified" and res["regression_case"]
    # a NEW conversation (different context) still follows it, because preferences are injected into every system prompt
    conv2 = chat.create()
    list(chat.send(conv2["id"], "How big is my drive?", use_retrieval=False))
    last_chat = [r for r in mock_lm.requests if "NEXUS AI" in r["messages"][0]["content"]][-1]      # (title requests use another prompt)
    assert "GiB" in last_chat["messages"][0]["content"]
    assert [m for m in lm_ctx.memory.search("disk sizes GiB", layers=["preference"])][0]["content"].endswith("never GB")
    suite = lm_ctx.evaluation.run_suite("after-feedback")
    assert suite["failed"] == 0 and suite["passed"] >= 1


def test_ambiguous_correction_triggers_clarifying_question_and_saves_nothing(lm_ctx, mock_lm):
    chat, conv, msg = _chat_with_answer(lm_ctx, mock_lm)
    res = lm_ctx.learning.submit_feedback(conv["id"], msg["id"], correction="The gateway port is actually 9443")
    assert res["needs_scope"] and "one-time" in res["question"] and set(res["options"]) == {"one_time", "preference", "fact", "hypothesis"}
    assert lm_ctx.memory.list() == []


def test_verified_fact_supersedes_old_memory_and_one_time_stays_local(lm_ctx, mock_lm):
    old = lm_ctx.memory.add("semantic", "The gateway listens on port 8080", trust="unverified")
    chat, conv, msg = _chat_with_answer(lm_ctx, mock_lm, "Which port does the gateway use?", "8080")
    r = lm_ctx.learning.submit_feedback(conv["id"], msg["id"], correction="The gateway listens on port 9443", scope="fact", replaces_memory_id=old["id"])
    assert lm_ctx.memory.get(old["id"])["status"] == "outdated"
    hits = lm_ctx.memory.search("gateway port", k=3)
    assert hits[0]["content"].endswith("9443") and old["id"] not in [h["id"] for h in hits]
    assert lm_ctx.evaluation.run_suite()["failed"] == 0
    # one-time corrections never leak to other conversations
    lm_ctx.learning.submit_feedback(conv["id"], msg["id"], correction="Use the word banana in this answer", scope="one_time")
    assert lm_ctx.memory.search("banana", conversation_id="other") == []
    assert lm_ctx.memory.search("banana", conversation_id=conv["id"])


def test_hypothesis_is_labelled_and_never_trusted(lm_ctx, mock_lm):
    chat, conv, msg = _chat_with_answer(lm_ctx, mock_lm)
    r = lm_ctx.learning.submit_feedback(conv["id"], msg["id"], correction="Maybe the backup job fails on Sundays", scope="hypothesis")
    m = lm_ctx.memory.get(r["saved"]["id"])
    assert m["trust"] == "hypothesis" and m["kind"] == "hypothesis" and m["confidence"] < 0.5


def test_thumbs_adjust_retrieval_utility(lm_ctx, tmp_path, mock_lm):
    write(tmp_path, "a.txt", "Disk usage can be measured with the df command on Linux systems and du for directories.")
    write(tmp_path, "b.txt", "Disk usage on Windows is shown in the Storage settings page and via the dir command.")
    job = lm_ctx.ingestion.create_job("u", "k", [str(tmp_path)])
    lm_ctx.ingestion.run(job["id"])
    chat = ChatService(lm_ctx)
    conv = chat.create()
    evs = list(chat.send(conv["id"], "How do I measure disk usage?"))
    msg = [e for e in evs if e["type"] == "done"][0]["message"]
    ids = msg["meta"]["chunk_ids"]
    assert len(ids) == 2
    before = [h["chunk_id"] for h in lm_ctx.kb.search("measure disk usage", k=2)["hits"]]
    for _ in range(4):                                                   # repeated thumbs-down on this answer
        lm_ctx.learning.submit_feedback(conv["id"], msg["id"], rating=-1)
    utils = {r["id"]: r["utility"] for r in lm_ctx.db.query("SELECT id, utility FROM chunks")}
    assert all(u < 0 for u in utils.values())
    lm_ctx.kb.record_utility([ids[1]], +8)                               # the other chunk is repeatedly marked helpful
    after = [h["chunk_id"] for h in lm_ctx.kb.search("measure disk usage", k=2)["hits"]]
    assert after[0] == ids[1]


def test_task_outcome_creates_episode_and_workflow_candidate_that_can_be_evaluated_and_approved(agent_ctx):
    ws = agent_ctx.settings.path("workspace")
    write(ws, "metrics.csv", "region,amount\neu,1\nus,2\neu,3\n")
    agent_ctx.llm.push(PLAN, act("csv_inspect", path="metrics.csv"), act("data_analyze", path="metrics.csv", operation="value_counts", column="region"),
                       final("There are 3 rows; eu appears twice."))
    t = agent_ctx.tasks.create("Summarise region counts in metrics.csv")
    t = wait_status(agent_ctx, t["id"], ("completed",))
    ep = agent_ctx.memory.list(layer="episodic")
    assert ep and "csv_inspect -> data_analyze" in ep[0]["content"] and ep[0]["trust"] == "unverified"
    cands = agent_ctx.skills.list("workflow")
    assert len(cands) == 1 and cands[0]["status"] == "validated", cands[0]["tests"]
    sk = cands[0]
    assert sk["spec"]["params"] == {"path": {"type": "string", "description": "path used in the original task"}} or "path" in sk["spec"]["params"]
    assert sk["tests"]["passed"] and sk["tests"]["replays"][0]["ok"]
    with pytest.raises(PermissionError):
        agent_ctx.skills.approve(sk["id"], by="agent")                       # the agent cannot approve its own skill
    agent_ctx.skills.approve(sk["id"])
    # run the learned workflow on a different file, through the normal permission checks
    write(ws, "other.csv", "region,amount\nx,1\nx,2\ny,3\n")
    from nexus.tools.registry import ToolContext
    r = agent_ctx.skills.run(sk["name"], {"path": "other.csv", "column": "region"}, ToolContext(app=agent_ctx))
    assert r["ok"] and r["result"]["top"][0] == {"value": "x", "count": 2}
    assert not agent_ctx.skills.run(sk["name"], {"path": "other.csv"}, ToolContext(app=agent_ctx))["ok"]       # missing parameter is reported
    # it is discoverable by future planning
    assert agent_ctx.skills.match("count regions in a csv file")[0]["name"] == sk["name"]
    t2_ctx = agent_ctx.tasks._context_for(agent_ctx.tasks.get(t["id"]))[0]
    assert sk["name"] in t2_ctx


def test_invalid_candidate_fails_validation_and_cannot_be_approved(agent_ctx):
    sk = agent_ctx.skills.create_candidate({"name": "bad_flow", "description": "x", "params": {}, "steps": [{"id": "a", "tool": "no_such_tool", "args": {}}]})
    assert sk["status"] == "failed_validation" and "unknown tool" in sk["tests"]["static_errors"][0]
    with pytest.raises(ValueError):
        agent_ctx.skills.approve(sk["id"])


def test_regression_suite_catches_a_broken_improvement(agent_ctx):
    m = agent_ctx.memory.add("preference", "Always reply in French", trust="user_verified")
    agent_ctx.evaluation.add_case("french kept", "prompt_contains", {"text": "Always reply in French"})
    agent_ctx.evaluation.add_case("french retrievable", "retrieval", {"query": "reply French", "expect_memory_ids": [m["id"]]})
    assert agent_ctx.evaluation.run_suite("baseline")["failed"] == 0
    agent_ctx.memory.delete(m["id"])                                         # a 'bad improvement' removes the preference
    run = agent_ctx.evaluation.run_suite("after-change")
    assert run["failed"] == 2 and set(run["regressions"]) == {"french kept", "french retrievable"}


def test_llm_answer_checks_are_skipped_not_passed_when_model_down(ctx):
    ctx.evaluation.add_case("answer", "llm_answer", {"question": "2+2?", "must_include": ["4"]})
    r = ctx.evaluation.run_suite()
    assert r["skipped"] == 1 and r["passed"] == 0


def test_three_learning_systems_are_reported_separately_and_weights_untouched(lm_ctx, mock_lm, tmp_path):
    write(tmp_path, "d.txt", "Some reference material about the nightly build pipeline and its owners in the platform team.")
    job = lm_ctx.ingestion.create_job("o", "k", [str(tmp_path / "d.txt")])
    lm_ctx.ingestion.run(job["id"])
    chat, conv, msg = _chat_with_answer(lm_ctx, mock_lm)
    lm_ctx.learning.submit_feedback(conv["id"], msg["id"], correction="Always be brief", scope="preference")
    ov = lm_ctx.learning.overview()
    assert ov["data_ingestion"]["chunks_indexed"] >= 1 and ov["persistent_learning"]["memories"]["preference"] == 1
    assert ov["model_weights"]["weights_modified"] is False and ov["model_weights"]["active_adapter"] is None
    assert "unmodified" in ov["model_weights"]["statement"]
    assert "Does not change the model" in ov["data_ingestion"]["what"] and "Does not change the model" in ov["persistent_learning"]["what"]
