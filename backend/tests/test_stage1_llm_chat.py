"""Stage 1: LM Studio integration + persistent chat (against the protocol-level mock)."""
import pytest

from nexus.agent.chat import ChatService
from nexus.core import AppContext
from nexus.llm.base import LLMError, ThinkFilter, strip_think
from nexus.llm.embeddings import HashEmbedder
from nexus.llm.lmstudio import LMStudioProvider


def test_connection_and_generation(mock_lm):
    p = LMStudioProvider(mock_lm.base_url)
    assert p.health()["ok"]
    r = p.chat([{"role": "user", "content": "hello"}])
    assert r.text.startswith("Mock reply to: hello")


def test_model_discovery_and_selection(mock_lm):
    p = LMStudioProvider(mock_lm.base_url)
    models = {m.id: m for m in p.list_models()}
    assert models["mock-chat"].loaded and not models["mock-chat-2"].loaded
    assert models["mock-chat"].max_context == 32768 and models["mock-embed"].type == "embeddings"
    assert p.resolve_model(None) == "mock-chat"                  # prefers a loaded chat model
    assert LMStudioProvider(mock_lm.base_url, default_model="mock-chat-2").resolve_model(None) == "mock-chat-2"


def test_streaming_and_load_unload(mock_lm):
    p = LMStudioProvider(mock_lm.base_url)
    mock_lm.replies.append("one two three")
    evs = list(p.stream_chat([{"role": "user", "content": "x"}]))
    assert "".join(e.text for e in evs if e.kind == "delta") == "one two three"
    assert evs[-1].kind == "done"
    assert p.load_model("mock-chat-2")["ok"] and mock_lm.loaded["mock-chat-2"]
    p.unload_model("mock-chat-2")
    assert not mock_lm.loaded["mock-chat-2"]


def test_capabilities_prompt_fallback_vs_native(mock_lm, mock_lm_native):
    caps = LMStudioProvider(mock_lm.base_url).capabilities()
    assert not caps.native_tools and any("fallback" in l for l in caps.limitations)
    p2 = LMStudioProvider(mock_lm_native.base_url)
    assert p2.capabilities().native_tools is True
    assert p2.probe_native_tools() is True
    # a model that does not advertise tool_use can be probed; a non-tool server answers without tool_calls
    p3 = LMStudioProvider(mock_lm.base_url)
    assert p3.probe_native_tools() is False


def test_failed_connection_gives_useful_error():
    p = LMStudioProvider("http://127.0.0.1:9/v1", timeout=2)
    h = p.health()
    assert not h["ok"] and h["code"] == "server_unreachable" and "Start Server" in h["hint"]
    with pytest.raises(LLMError) as e:
        p.chat([{"role": "user", "content": "hi"}])
    assert e.value.code == "server_unreachable"


def test_unavailable_model_and_auth_errors(mock_lm):
    p = LMStudioProvider(mock_lm.base_url)
    with pytest.raises(LLMError) as e:
        p.chat([{"role": "user", "content": "hi"}], model="nope")
    assert e.value.code == "model_unavailable"
    mock_lm.api_key = "secret"
    with pytest.raises(LLMError) as e2:
        LMStudioProvider(mock_lm.base_url).chat([{"role": "user", "content": "hi"}])
    assert e2.value.code == "auth_failed"
    assert LMStudioProvider(mock_lm.base_url, api_key="secret").health()["ok"]


def test_think_tags_removed_from_stream_and_text():
    assert strip_think("<think>secret reasoning</think>\nAnswer") == "Answer"
    f = ThinkFilter()
    out = "".join(f.feed(c) for c in "<th" + "ink>hidden</thi" + "nk>Visible <b>ok</b>") + f.flush()
    assert out == "Visible <b>ok</b>"


def test_lmstudio_embedder(lm_ctx, mock_lm):
    from nexus.llm.embeddings import LMStudioEmbedder
    e = LMStudioEmbedder(lm_ctx.provider, "mock-embed")
    v = e.embed(["alpha beta", "gamma"])
    assert v.shape == (2, 64) and e.dim == 64


def test_multi_turn_persistent_chat_and_restart(settings, mock_lm):
    settings.lmstudio_base_url = mock_lm.base_url
    ctx1 = AppContext(settings, embedder=HashEmbedder())
    chat = ChatService(ctx1)
    conv = chat.create()
    for text in ["First question", "Second question", "Third one"]:
        evs = list(chat.send(conv["id"], text, use_retrieval=False))
        assert evs[-2]["type"] == "done" or any(e["type"] == "done" for e in evs)
    # history reaches the model on turn 3
    last_req = mock_lm.requests[-1]["messages"] if mock_lm.requests else []
    roles = [m["role"] for m in mock_lm.requests[-3]["messages"]] if len(mock_lm.requests) >= 3 else []
    assert "assistant" in roles or any(m["role"] == "assistant" for m in last_req)
    # restart: brand-new context on the same data dir sees everything
    ctx2 = AppContext(settings, embedder=HashEmbedder())
    chat2 = ChatService(ctx2)
    msgs = chat2.messages(conv["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 3
    assert chat2.get(conv["id"])["title"] != "New conversation"
    list(chat2.send(conv["id"], "Fourth after restart", use_retrieval=False))
    assert len(chat2.messages(conv["id"])) == 8


def test_chat_error_event_when_server_down(ctx):
    chat = ChatService(ctx)
    conv = chat.create()
    evs = list(chat.send(conv["id"], "hello", use_retrieval=False))
    assert evs[-1]["type"] == "error" and evs[-1]["error"]["code"] == "server_unreachable"
    assert chat.messages(conv["id"])[0]["role"] == "user"       # the user's message is not lost


def test_remember_command_and_preference_in_prompt(lm_ctx, mock_lm):
    chat = ChatService(lm_ctx)
    conv = chat.create()
    list(chat.send(conv["id"], "Remember that my project is called Falcon", use_retrieval=False))
    prefs_before = lm_ctx.memory.list(layer="semantic")
    assert any("Falcon" in m["content"] and m["trust"] == "user_verified" for m in prefs_before)
    list(chat.send(conv["id"], "From now on answer in bullet points", use_retrieval=False))
    list(chat.send(conv["id"], "What next?", use_retrieval=False))
    system = mock_lm.requests[-1]["messages"][0]["content"]
    assert "bullet points" in system


def test_rolling_summary_keeps_prompt_small(lm_ctx, mock_lm):
    lm_ctx.settings.summarize_after = 6
    lm_ctx.settings.history_messages = 4
    chat = ChatService(lm_ctx)
    conv = chat.create()
    for i in range(8):
        list(chat.send(conv["id"], f"message number {i} about topic {i}", use_retrieval=False))
    c = chat.get(conv["id"])
    assert c["summary"] and c["summarized_upto"] > 0
    assert len(mock_lm.requests[-1]["messages"]) <= 1 + 4 + 1     # system + history window (+ current)
