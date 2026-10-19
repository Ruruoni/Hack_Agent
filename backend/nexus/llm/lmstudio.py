"""LM Studio adapter (OpenAI-compatible API + LM Studio's richer native REST endpoints when present)."""
from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from .base import (Capabilities, ChatEvent, ChatResult, LLMError, LLMProvider, ModelInfo, ThinkFilter, ToolCall,
                   strip_think)

# Inference parameters we pass through when provided. Others are dropped rather than risking a 400.
SUPPORTED_PARAMS = {"temperature", "top_p", "max_tokens", "stop", "seed", "presence_penalty",
                    "frequency_penalty", "response_format", "logit_bias"}


class LMStudioProvider(LLMProvider):
    name = "lmstudio"

    def __init__(self, base_url: str = "http://localhost:1234/v1", api_key: str = "", timeout: float = 120.0,
                 default_model: str = "", tool_mode: str = "auto", embedding_model: str = "",
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        self.api_key = api_key
        self.timeout = timeout
        self.default_model = default_model
        self.tool_mode = tool_mode
        self.embedding_model = embedding_model
        self._transport = transport
        self._probe_cache: dict[str, bool] = {}

    # -- plumbing ----------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(headers=self._headers(), timeout=timeout or self.timeout, transport=self._transport)

    def _unreachable(self, e: Exception) -> LLMError:
        return LLMError("server_unreachable", f"Cannot reach LM Studio at {self.base_url}: {e.__class__.__name__}",
                        "Open LM Studio, go to the Developer tab, load a model and click 'Start Server'. "
                        "Check the port (default 1234) matches NEXUS_LMSTUDIO_BASE_URL.")

    def _raise_for(self, r: httpx.Response, model: str | None) -> None:
        if r.status_code < 400:
            return
        body = r.text[:500]
        low = body.lower()
        if r.status_code in (401, 403):
            raise LLMError("auth_failed", f"LM Studio rejected the API key (HTTP {r.status_code}).",
                           "Set NEXUS_LMSTUDIO_API_KEY (or Settings > Model endpoint) to match the server setting.")
        if "model" in low and any(w in low for w in ("not found", "not loaded", "no model", "failed to load", "unknown")):
            raise LLMError("model_unavailable", f"Model '{model or '(default)'}' is not available: {body}",
                           "Download/load the model in LM Studio (or enable Just-In-Time loading) and pick it in Models.")
        if r.status_code == 404:
            raise LLMError("endpoint_missing", f"Endpoint not found ({r.request.url}).",
                           "This server may not be LM Studio or its API version differs; check the base URL ends with /v1.")
        raise LLMError("http_error", f"LM Studio returned HTTP {r.status_code}: {body}")

    # -- health / models ---------------------------------------------------------------
    def health(self) -> dict:
        try:
            with self._client(8) as c:
                r = c.get(f"{self.base_url}/models")
            self._raise_for(r, None)
            ids = [m.get("id") for m in r.json().get("data", [])]
            return {"ok": True, "base_url": self.base_url, "models": len(ids), "api": "openai-compatible"}
        except LLMError as e:
            return {"ok": False, "base_url": self.base_url, **e.to_dict()}
        except (httpx.HTTPError, ValueError) as e:
            return {"ok": False, "base_url": self.base_url, **self._unreachable(e).to_dict()}

    def list_models(self) -> list[ModelInfo]:
        try:
            with self._client(10) as c:
                r = c.get(f"{self.root}/api/v0/models")
                if r.status_code == 200:
                    out = []
                    for m in r.json().get("data", []):
                        raw_type = str(m.get("type", "llm")).lower()
                        out.append(ModelInfo(
                            id=m.get("id", ""), type="embedding" if "embed" in raw_type else raw_type,
                            loaded=m.get("state") == "loaded" if "state" in m else None,
                            max_context=m.get("max_context_length"), loaded_context=m.get("loaded_context_length"),
                            arch=m.get("arch", ""), quantization=m.get("quantization", ""),
                            publisher=m.get("publisher", ""), capabilities=list(m.get("capabilities") or [])))
                    return out
                r = c.get(f"{self.base_url}/models")
            self._raise_for(r, None)
            out = []
            for m in r.json().get("data", []):
                mid = m.get("id", "")
                typ = "embedding" if "embed" in mid.lower() else "llm"
                out.append(ModelInfo(id=mid, type=typ))
            return out
        except LLMError:
            raise
        except (httpx.HTTPError, ValueError) as e:
            raise self._unreachable(e)

    def resolve_model(self, model: str | None, kind: str = "llm") -> str:
        if model:
            return model
        if kind == "llm" and self.default_model:
            return self.default_model
        models = [m for m in self.list_models() if (m.type != "embedding") == (kind == "llm")]
        loaded = [m for m in models if m.loaded]
        pick = (loaded or models)
        if not pick:
            raise LLMError("no_models", "LM Studio has no models available.",
                           "Download a chat model in LM Studio (Discover tab) and load it.")
        return pick[0].id

    def capabilities(self, model: str | None = None) -> Capabilities:
        mid = self.resolve_model(model)
        info = next((m for m in self.list_models() if m.id == mid), None)
        lims: list[str] = []
        native: bool | None = None
        if self.tool_mode == "native":
            native = True
        elif self.tool_mode == "prompt":
            native = False
        elif info and "tool_use" in info.capabilities:
            native = True
        elif mid in self._probe_cache:
            native = self._probe_cache[mid]
        if not native:
            lims.append("No native tool calling detected: the agent uses a validated JSON-action fallback.")
        vision = bool(info and info.type == "vlm")
        if not vision:
            lims.append("Text-only model: image inputs are not supported.")
        ctx = (info.loaded_context or info.max_context) if info else None
        if ctx and ctx < 4096:
            lims.append(f"Small context window ({ctx} tokens): retrieval and summaries are trimmed aggressively.")
        if info and info.loaded is False:
            lims.append("Model is downloaded but not loaded; the first request may be slow (or fail if JIT loading is off).")
        return Capabilities(model=mid, native_tools=native, vision=vision, structured_output=None,
                            max_context=info.max_context if info else None,
                            loaded_context=info.loaded_context if info else None, limitations=lims)

    def probe_native_tools(self, model: str | None = None) -> bool:
        """Actively test native function calling once and cache the result."""
        mid = self.resolve_model(model)
        tools = [{"type": "function", "function": {"name": "ping", "description": "Call this to reply.",
                                                    "parameters": {"type": "object", "properties": {}}}}]
        try:
            res = self.chat([{"role": "user", "content": "Call the ping tool now."}], model=mid, tools=tools,
                            max_tokens=64, temperature=0)
            ok = any(tc.name == "ping" for tc in res.tool_calls)
        except LLMError:
            ok = False
        self._probe_cache[mid] = ok
        return ok

    def uses_native_tools(self, model: str | None = None) -> bool:
        return bool(self.capabilities(model).native_tools)

    # -- chat --------------------------------------------------------------------------
    def _payload(self, messages: list[dict], model: str | None, tools: list[dict] | None, stream: bool,
                 params: dict[str, Any]) -> dict:
        p: dict[str, Any] = {"model": self.resolve_model(model), "messages": messages, "stream": stream}
        for k, v in params.items():
            if k in SUPPORTED_PARAMS and v is not None:
                p[k] = v
        if tools:
            p["tools"] = tools
        return p

    @staticmethod
    def _parse_calls(raw: list[dict] | None) -> list[ToolCall]:
        out = []
        for i, tc in enumerate(raw or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    args = {"_raw": args}
            out.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args))
        return out

    def chat(self, messages: list[dict], *, model: str | None = None, tools: list[dict] | None = None,
             **params: Any) -> ChatResult:
        payload = self._payload(messages, model, tools, False, params)
        try:
            with self._client() as c:
                r = c.post(f"{self.base_url}/chat/completions", json=payload)
        except httpx.TimeoutException as e:
            raise LLMError("timeout", f"LM Studio did not answer within {self.timeout:.0f}s.",
                           "Use a smaller model/context, or raise the request timeout in Settings.") from e
        except httpx.HTTPError as e:
            raise self._unreachable(e) from e
        self._raise_for(r, payload["model"])
        try:
            data = r.json()
            ch = data["choices"][0]
            msg = ch["message"]
        except (ValueError, KeyError, IndexError) as e:
            raise LLMError("bad_response", "LM Studio returned an unexpected response shape.",
                           "Check the server is LM Studio's OpenAI-compatible API.") from e
        return ChatResult(text=strip_think(msg.get("content") or ""), tool_calls=self._parse_calls(msg.get("tool_calls")),
                          finish_reason=ch.get("finish_reason") or "", usage=data.get("usage") or {},
                          model=data.get("model", payload["model"]))

    def stream_chat(self, messages: list[dict], *, model: str | None = None, tools: list[dict] | None = None,
                    **params: Any) -> Iterator[ChatEvent]:
        payload = self._payload(messages, model, tools, True, params)
        flt = ThinkFilter()
        text_parts: list[str] = []
        calls: dict[int, dict] = {}
        finish = ""
        usage: dict = {}
        try:
            with self._client() as c:
                with c.stream("POST", f"{self.base_url}/chat/completions", json=payload) as r:
                    if r.status_code >= 400:
                        r.read()
                        self._raise_for(r, payload["model"])
                    for line in r.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            ch = obj["choices"][0]
                        except (ValueError, KeyError, IndexError):
                            continue
                        usage = obj.get("usage") or usage
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            out = flt.feed(delta["content"])
                            if out:
                                text_parts.append(out)
                                yield ChatEvent("delta", text=out)
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {"id": "", "function": {"name": "", "arguments": ""}})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["function"]["name"] += fn.get("name") or ""
                            slot["function"]["arguments"] += fn.get("arguments") or ""
                        finish = ch.get("finish_reason") or finish
        except httpx.TimeoutException as e:
            raise LLMError("timeout", "LM Studio stream timed out.", "Try a smaller model or raise the timeout.") from e
        except httpx.HTTPError as e:
            raise self._unreachable(e) from e
        tail = flt.flush()
        if tail:
            text_parts.append(tail)
            yield ChatEvent("delta", text=tail)
        tcs = self._parse_calls([calls[k] for k in sorted(calls)])
        if tcs:
            yield ChatEvent("tool_calls", tool_calls=tcs)
        yield ChatEvent("done", result=ChatResult(text="".join(text_parts), tool_calls=tcs, finish_reason=finish,
                                                  usage=usage, model=payload["model"]))

    # -- embeddings --------------------------------------------------------------------
    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        mid = model or self.embedding_model or self.resolve_model(None, "embedding")
        try:
            with self._client() as c:
                r = c.post(f"{self.base_url}/embeddings", json={"model": mid, "input": texts})
        except httpx.HTTPError as e:
            raise self._unreachable(e) from e
        self._raise_for(r, mid)
        try:
            items = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            return [d["embedding"] for d in items]
        except (ValueError, KeyError) as e:
            raise LLMError("bad_response", "Unexpected /embeddings response.") from e

    # -- load / unload (LM Studio >= 0.4 REST API; older builds: use the `lms` CLI) ----------
    def load_model(self, model: str, context_length: int | None = None) -> dict:
        body: dict[str, Any] = {"model": model}
        if context_length:
            body["context_length"] = context_length
        return self._native_post("/api/v1/models/load", body, "load")

    def unload_model(self, model: str) -> dict:
        return self._native_post("/api/v1/models/unload", {"instance_id": model}, "unload")

    def _native_post(self, path: str, body: dict, verb: str) -> dict:
        try:
            with self._client(300) as c:
                r = c.post(f"{self.root}{path}", json=body)
        except httpx.HTTPError as e:
            raise self._unreachable(e) from e
        if r.status_code in (404, 405):
            raise LLMError("unsupported", f"This LM Studio version has no REST endpoint to {verb} models.",
                           f"Use the LM Studio UI or CLI: `lms {verb} <model>`; or enable Just-In-Time loading.")
        self._raise_for(r, body.get("model"))
        try:
            return r.json()
        except ValueError:
            return {"ok": True}
