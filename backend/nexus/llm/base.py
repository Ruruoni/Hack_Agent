"""Provider-neutral LLM interface. The agent only ever talks to `LLMProvider`."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator


class LLMError(Exception):
    """Raised for every provider failure; `code` is stable, `hint` is a human-readable fix."""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint}


@dataclass
class ModelInfo:
    id: str
    type: str = "llm"                  # llm | vlm | embedding
    loaded: bool | None = None         # None = unknown (OpenAI-compat endpoint only)
    max_context: int | None = None
    loaded_context: int | None = None
    arch: str = ""
    quantization: str = ""
    publisher: str = ""
    capabilities: list[str] = field(default_factory=list)   # e.g. ["tool_use"]

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""


@dataclass
class ChatEvent:
    kind: str                           # delta | tool_calls | done
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    result: ChatResult | None = None


@dataclass
class Capabilities:
    model: str
    native_tools: bool | None           # None = unknown, will use prompt fallback
    vision: bool = False
    structured_output: bool | None = None
    max_context: int | None = None
    loaded_context: int | None = None
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class LLMProvider(ABC):
    name = "base"

    @abstractmethod
    def health(self) -> dict: ...

    @abstractmethod
    def list_models(self) -> list[ModelInfo]: ...

    @abstractmethod
    def chat(self, messages: list[dict], *, model: str | None = None, tools: list[dict] | None = None,
             **params: Any) -> ChatResult: ...

    @abstractmethod
    def stream_chat(self, messages: list[dict], *, model: str | None = None,
                    tools: list[dict] | None = None, **params: Any) -> Iterator[ChatEvent]: ...

    @abstractmethod
    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]: ...

    def capabilities(self, model: str | None = None) -> Capabilities:
        return Capabilities(model=model or "", native_tools=None)

    def load_model(self, model: str, context_length: int | None = None) -> dict:
        raise LLMError("unsupported", "This provider cannot load models via API.")

    def unload_model(self, model: str) -> dict:
        raise LLMError("unsupported", "This provider cannot unload models via API.")


_THINK = re.compile(r"<think>.*?</think>", re.S)


def strip_think(text: str) -> str:
    """Remove reasoning-model <think> blocks (also an unterminated trailing one)."""
    text = _THINK.sub("", text)
    i = text.find("<think>")
    if i != -1:
        text = text[:i]
    return text.lstrip("\n") if text.startswith("\n") else text


class ThinkFilter:
    """Incrementally drops <think>...</think> from a token stream."""

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False

    def feed(self, s: str) -> str:
        self.buf += s
        out = ""
        while True:
            if self.inside:
                j = self.buf.find("</think>")
                if j == -1:
                    self.buf = self.buf[-8:]
                    return out
                self.buf = self.buf[j + 8:].lstrip("\n")
                self.inside = False
            else:
                i = self.buf.find("<think>")
                if i == -1:
                    keep = 0
                    for n in range(1, 7):                    # hold back a possible partial "<think>"
                        if "<think>".startswith(self.buf[-n:]) and len(self.buf) >= n:
                            keep = n
                    out += self.buf[: len(self.buf) - keep]
                    self.buf = self.buf[len(self.buf) - keep:]
                    return out
                out += self.buf[:i]
                self.buf = self.buf[i + 7:]
                self.inside = True

    def flush(self) -> str:
        out = "" if self.inside else self.buf
        self.buf = ""
        return out


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.6))
