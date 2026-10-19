"""A stand-in for LM Studio's local server used by the test-suite.

It speaks the same HTTP/SSE protocol (OpenAI-compatible + /api/v0/models) so the real LMStudioProvider code
is exercised. It is NOT a language model: replies are scripted or templated. Tests that pass against it prove
the integration code works; they say nothing about the quality of any real model.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time

import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from nexus.llm.embeddings import HashEmbedder


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockLM:
    def __init__(self, native_tools: bool = False):
        self.native_tools = native_tools
        self.replies: list[str] = []          # scripted replies, consumed in order
        self.requests: list[dict] = []
        self.fail_with: int | None = None
        self.api_key: str | None = None
        self.loaded = {"mock-chat": True, "mock-chat-2": False}
        self.emb = HashEmbedder(64)
        self.app = self._build()
        self.port = _free_port()
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _reply_for(self, body: dict) -> str:
        if self.replies:
            return self.replies.pop(0)
        msgs = body.get("messages", [])
        system = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
        last = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        m = re.search(r'<untrusted_source id="S1"[^>]*>\n(.*?)\n</untrusted_source>', system, re.S)
        if m:
            return f"According to the sources: {m.group(1)[:160]} [S1]"
        return f"Mock reply to: {last[:100]}"

    def _build(self) -> FastAPI:
        app = FastAPI()

        def guard(req: Request):
            if self.api_key and req.headers.get("authorization") != f"Bearer {self.api_key}":
                return JSONResponse({"error": "invalid api key"}, status_code=401)
            if self.fail_with:
                return JSONResponse({"error": {"message": "model not loaded: failed to load model"}}, status_code=self.fail_with)
            return None

        @app.get("/v1/models")
        async def models(req: Request):
            if (g := guard(req)):
                return g
            return {"data": [{"id": k, "object": "model"} for k in self.loaded] + [{"id": "mock-embed", "object": "model"}]}

        @app.get("/api/v0/models")
        async def models_v0(req: Request):
            if (g := guard(req)):
                return g
            caps = ["tool_use"] if self.native_tools else []
            data = [{"id": k, "type": "llm", "state": "loaded" if v else "not-loaded", "max_context_length": 32768,
                     "loaded_context_length": 8192 if v else None, "arch": "mock", "quantization": "Q4_K_M",
                     "publisher": "test", "capabilities": caps} for k, v in self.loaded.items()]
            data.append({"id": "mock-embed", "type": "embeddings", "state": "loaded", "max_context_length": 2048})
            return {"data": data}

        @app.post("/v1/chat/completions")
        async def chat(req: Request):
            if (g := guard(req)):
                return g
            body = await req.json()
            self.requests.append(body)
            if body["model"] not in self.loaded:
                return JSONResponse({"error": {"message": f"model '{body['model']}' not found"}}, status_code=404)
            tools = body.get("tools")
            if tools and self.native_tools and "ping" in json.dumps(tools):
                call = {"id": "c1", "type": "function", "function": {"name": "ping", "arguments": "{}"}}
                if body.get("stream"):
                    def gen_t():
                        yield "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, **call}]}, "finish_reason": None}]}) + "\n\n"
                        yield "data: [DONE]\n\n"
                    return StreamingResponse(gen_t(), media_type="text/event-stream")
                return {"model": body["model"], "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]},
                                                               "finish_reason": "tool_calls"}]}
            text = self._reply_for(body)
            if body.get("stream"):
                def gen():
                    words = re.findall(r"\S+\s*", text)
                    for w in words:
                        time.sleep(0.002)
                        yield "data: " + json.dumps({"choices": [{"delta": {"content": w}, "finish_reason": None}]}) + "\n\n"
                    yield "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": len(words)}}) + "\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(gen(), media_type="text/event-stream")
            return {"model": body["model"], "usage": {"total_tokens": len(text.split())},
                    "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}

        @app.post("/v1/embeddings")
        async def embeddings(req: Request):
            if (g := guard(req)):
                return g
            body = await req.json()
            inp = body["input"] if isinstance(body["input"], list) else [body["input"]]
            vecs = self.emb.embed(inp)
            return {"data": [{"index": i, "embedding": v.tolist()} for i, v in enumerate(vecs)], "model": body["model"]}

        @app.post("/api/v1/models/load")
        async def load(req: Request):
            body = await req.json()
            if body["model"] not in self.loaded:
                return JSONResponse({"error": {"message": "model not found"}}, status_code=404)
            self.loaded[body["model"]] = True
            return {"ok": True, "model": body["model"]}

        @app.post("/api/v1/models/unload")
        async def unload(req: Request):
            body = await req.json()
            self.loaded[body["instance_id"]] = False
            return {"ok": True}
        return app

    def start(self) -> "MockLM":
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.1).close()
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("mock LM Studio did not start")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
            self.thread.join(timeout=5)  # type: ignore[union-attr]
