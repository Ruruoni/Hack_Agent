"""Tool registry. A tool = metadata + schema + handler. New tools are added by registering them; the core
agent never needs editing. Plugins are plain Python files in data/extensions/active (placed there only by the
approval workflow in skills/)."""
from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("nexus.tools")


@dataclass
class ToolContext:
    app: Any                          # AppContext
    task_id: str | None = None
    cancelled: Callable[[], bool] = lambda: False
    log: Callable[[str], None] = lambda m: None
    workdir: Path | None = None


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[ToolContext, dict], dict]
    effects: Any = field(default_factory=lambda: {"read"})   # set[str] or callable(ctx, args) -> set[str]
    risk: str = "low"
    output_schema: dict = field(default_factory=lambda: {"type": "object"})
    dependencies: list[str] = field(default_factory=list)    # "module" or "exe:name"
    timeout: int = 60
    max_output_chars: int = 12000
    docs: str = ""
    targets_fn: Callable[[dict], list[str]] | None = None    # hosts the call would touch
    verify_fn: Callable[[ToolContext, dict, dict], tuple[bool, str]] | None = None
    untrusted_output: bool = False   # output is third-party content -> fence + injection scan
    source: str = "builtin"
    version: int = 1

    def effect_set(self, ctx: ToolContext, args: dict) -> set[str]:
        e = self.effects(ctx, args) if callable(self.effects) else self.effects
        return set(e)

    def missing_dependencies(self) -> list[str]:
        miss = []
        for d in self.dependencies:
            if d.startswith("exe:"):
                if not shutil.which(d[4:]):
                    miss.append(d)
            else:
                try:
                    if importlib.util.find_spec(d) is None:
                        miss.append(d)
                except (ImportError, ValueError):
                    miss.append(d)
        return miss

    def describe(self) -> dict:
        eff = sorted(self.effects) if not callable(self.effects) else ["(depends on arguments)"]
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema,
                "output_schema": self.output_schema, "effects": eff, "risk": self.risk, "timeout": self.timeout,
                "dependencies": self.dependencies, "missing_dependencies": self.missing_dependencies(),
                "docs": self.docs or self.description, "source": self.source, "version": self.version,
                "max_output_chars": self.max_output_chars}

    def openai_spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                  "parameters": self.input_schema}}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool '{tool.name}' already registered")
        if tool.risk not in ("low", "medium", "high", "critical"):
            raise ValueError("risk must be low|medium|high|critical")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def available(self) -> list[Tool]:
        return [t for t in self.all() if not t.missing_dependencies()]

    def catalog(self, only_available: bool = True) -> str:
        """Compact text catalogue for prompts."""
        lines = []
        for t in (self.available() if only_available else self.all()):
            props = t.input_schema.get("properties", {})
            req = set(t.input_schema.get("required", []))
            sig = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'any')}" for k, v in props.items())
            lines.append(f"- {t.name}({sig}) - {t.description}")
        return "\n".join(lines)

    def load_plugins(self, folder: Path) -> list[dict]:
        """Load approved plugin files. Each defines `register(registry)`. Failures are reported, never fatal."""
        results = []
        if not folder.is_dir():
            return results
        for f in sorted(folder.glob("*.py")):
            try:
                spec = importlib.util.spec_from_file_location(f"nexus_plugin_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                sys.modules[spec.name] = mod                 # type: ignore[union-attr]
                spec.loader.exec_module(mod)                 # type: ignore[union-attr]
                before = set(self._tools)
                mod.register(self)
                for n in set(self._tools) - before:
                    self._tools[n].source = f"plugin:{f.name}"
                results.append({"file": f.name, "ok": True, "tools": sorted(set(self._tools) - before)})
            except Exception as e:  # noqa: BLE001
                log.warning("plugin %s failed: %s", f, e)
                results.append({"file": f.name, "ok": False, "error": f"{e.__class__.__name__}: {e}"})
        return results


def ok(**data: Any) -> dict:
    return {"ok": True, **data}


def fail(msg: str, **data: Any) -> dict:
    return {"ok": False, "error": msg, **data}


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]", True
