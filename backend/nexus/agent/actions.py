"""Turns model output into a validated Action. Works with native tool calling and, for models without it, a strict
JSON-action protocol. Nothing here executes anything: the orchestrator validates and runs actions through the
permission engine afterwards."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..llm.base import LLMError, LLMProvider
from ..tools.registry import Tool


class ActionParseError(Exception):
    pass


@dataclass
class Action:
    kind: str                       # tool | final | ask
    tool: str = ""
    args: dict = field(default_factory=dict)
    text: str = ""
    reason: str = ""
    raw: str = ""


def extract_json_object(text: str) -> dict:
    """First balanced {...} in text (handles ```json fences, prose before/after, braces inside strings)."""
    t = text.strip()
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = t.find("{", start + 1)
    raise ActionParseError("no valid JSON object found in the reply")


def parse_action(text: str) -> Action:
    obj = extract_json_object(text)
    kind = str(obj.get("action") or ("tool" if "tool" in obj else "final" if "answer" in obj else "ask" if "question" in obj else "")).lower()
    if kind in ("tool", "call", "use_tool"):
        name = obj.get("tool") or obj.get("name")
        args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or {}
        if not isinstance(name, str) or not name:
            raise ActionParseError("action 'tool' needs a string field 'tool'")
        if not isinstance(args, dict):
            raise ActionParseError("'args' must be a JSON object")
        return Action("tool", tool=name, args=args, reason=str(obj.get("reason", ""))[:300], raw=text)
    if kind in ("final", "finish", "answer", "done"):
        ans = obj.get("answer") or obj.get("text") or obj.get("result")
        if not isinstance(ans, str) or not ans.strip():
            raise ActionParseError("action 'final' needs a non-empty string field 'answer'")
        return Action("final", text=ans, raw=text)
    if kind in ("ask", "clarify", "question"):
        q = obj.get("question") or obj.get("text")
        if not isinstance(q, str) or not q.strip():
            raise ActionParseError("action 'ask' needs a string field 'question'")
        return Action("ask", text=q, raw=text)
    raise ActionParseError("field 'action' must be one of: tool, final, ask")


PROTOCOL = """Reply with EXACTLY ONE JSON object and nothing else. Allowed forms:
{"action":"tool","tool":"<tool name>","args":{...},"reason":"<one short sentence>"}
{"action":"ask","question":"<one concise question, only if essential information is missing>"}
{"action":"final","answer":"<your final report to the user>"}
Take ONE action per reply. Use only tools from the catalog, with arguments matching their signatures."""


class ActionProtocol:
    def __init__(self, provider: LLMProvider, settings):
        self.provider, self.s = provider, settings

    def native(self) -> bool:
        if self.s.tool_mode == "prompt":
            return False
        try:
            return bool(self.provider.capabilities().native_tools)  # type: ignore[attr-defined]
        except (LLMError, AttributeError):
            return False

    def next_action(self, messages: list[dict], tools: list[Tool], repairs: int = 2) -> tuple[Action, dict]:
        """Returns (action, info). Raises ActionParseError after exhausting repair attempts, LLMError on provider failure."""
        native = self.native()
        info: dict[str, Any] = {"mode": "native" if native else "prompt-json"}
        msgs = list(messages)
        last_err = ""
        for attempt in range(repairs + 1):
            if native:
                res = self.provider.chat(msgs, tools=[t.openai_spec() for t in tools], temperature=0.2, max_tokens=self.s.max_tokens)
                if res.tool_calls:
                    tc = res.tool_calls[0]
                    info["extra_tool_calls_ignored"] = len(res.tool_calls) - 1
                    return Action("tool", tool=tc.name, args=tc.arguments if isinstance(tc.arguments, dict) else {}, raw=res.text), info
                text = res.text
            else:
                res = self.provider.chat(msgs, temperature=0.2, max_tokens=self.s.max_tokens)
                text = res.text
            try:
                return parse_action(text), info
            except ActionParseError as e:
                last_err = str(e)
                if native and text.strip() and attempt == 0:
                    return Action("final", text=text.strip(), raw=text), info      # plain prose from a native-tool model = final answer
                msgs = msgs + [{"role": "assistant", "content": text[:1500]},
                               {"role": "user", "content": f"Your reply was not a valid action ({last_err}). {PROTOCOL}"}]
        raise ActionParseError(last_err)
