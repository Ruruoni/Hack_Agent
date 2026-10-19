"""Application context: builds and wires every service. One instance per process (or per test)."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .config import Settings, load_settings
from .database.db import Database
from .events import EventBus
from .ingestion.pipeline import IngestionManager
from .knowledge.store import KnowledgeBase
from .knowledge.vectorstore import VectorStore, build_vector_store
from .llm.base import LLMProvider
from .llm.embeddings import Embedder, build_embedder
from .llm.lmstudio import LMStudioProvider
from .memory.store import MemoryStore
from .system import hardware

log = logging.getLogger("nexus.core")


class AppContext:
    def __init__(self, settings: Settings | None = None, provider: LLMProvider | None = None,
                 embedder: Embedder | None = None):
        self.settings = settings or load_settings()
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.db.init()
        self.bus = EventBus()
        self.stop_event = threading.Event()      # the global stop button
        self._provider_override = provider
        self._embedder_override = embedder
        self._provider: LLMProvider | None = provider
        self._embedder: Embedder | None = embedder
        self._embedder_key: tuple | None = None
        self.vectors: VectorStore = build_vector_store(self.settings, self.db)
        self.kb = KnowledgeBase(self.db, self.vectors, lambda: self.embedder)
        self.memory = MemoryStore(self.db, self.vectors, lambda: self.embedder)
        self.ingestion = IngestionManager(self.settings, self.db, self.kb, lambda: self.embedder, self.stop_event,
                                          on_event=self.bus.publish)
        self._late_init()
        interrupted = self.ingestion.recover_interrupted()
        if interrupted:
            log.warning("Recovered %d interrupted ingestion job(s): %s", len(interrupted), interrupted)
        self.db.audit("system", "app.start", {"vector_backend": self.vectors.name})

    def _late_init(self) -> None:
        from .permissions.policy import PermissionEngine
        from .skills.manager import SkillManager
        from .tasks.manager import TaskManager
        from .tools.builtin import register_all
        from .tools.executor import ToolExecutor
        from .tools.registry import Tool, ToolContext, fail
        from .web_research.research import ResearchManager
        self.perms = PermissionEngine(self.settings, self.db)
        self.tools = __import__("nexus.tools.registry", fromlist=["ToolRegistry"]).ToolRegistry()
        self.executor = ToolExecutor(self)
        self.research = ResearchManager(self.settings, self.db, self.stop_event, on_event=self.bus.publish)
        self.ingestion.url_fetcher = self.research.url_fetcher
        from .datasets.manager import DatasetManager
        self.datasets = DatasetManager(self.settings, self.db, self.ingestion, on_event=self.bus.publish)
        register_all(self.tools, self)
        self.skills = SkillManager(self)
        self._register_skill_tools(Tool, ToolContext, fail)
        self.tools.load_plugins(self.settings.path("extensions", "active"))
        self.tasks = TaskManager(self)
        self.research.recover()
        n = self.tasks.recover()
        if n:
            log.warning("Marked %d interrupted task(s) as resumable", n)

    def _register_skill_tools(self, Tool, ToolContext, fail) -> None:
        S = lambda **p: {"type": "object", "properties": p, "additionalProperties": False}  # noqa: E731

        def run_skill(ctx, a):
            return self.skills.run(a["name"], a.get("params") or {}, ctx)

        def propose_extension(ctx, a):
            p = self.skills.propose_extension(a["capability"], a.get("rationale", ""), origin=f"task:{ctx.task_id}", dependencies=a.get("dependencies"))
            return {"ok": True, "proposal_id": p["id"], "status": p["status"],
                    "note": "Saved as a PROPOSAL. It is inactive; a human must test and approve it in Skills > Extensions."}
        self.tools.register(Tool("run_skill", "Run an approved reusable workflow skill by name. Each step is still permission-checked.",
                                 {**S(name={"type": "string"}, params={"type": "object"}), "required": ["name"]}, run_skill,
                                 effects={"read"}, risk="medium", timeout=900))
        self.tools.register(Tool("propose_extension", "Record that a needed capability is missing and draft an extension proposal for a human to review.",
                                 {**S(capability={"type": "string"}, rationale={"type": "string"}, dependencies={"type": "array", "items": {"type": "string"}}),
                                  "required": ["capability"]}, propose_extension, effects={"write_scratch"}, risk="low"))

    # -- provider / embedder (rebuilt when settings change) ----------------------------------
    @property
    def provider(self) -> LLMProvider:
        if self._provider_override is not None:
            return self._provider_override
        s = self.settings
        key = (s.lmstudio_base_url, s.lmstudio_api_key, s.chat_model, s.tool_mode, s.request_timeout, s.embedding_model)
        if self._provider is None or getattr(self, "_provider_key", None) != key:
            self._provider = LMStudioProvider(s.lmstudio_base_url, s.lmstudio_api_key, s.request_timeout, s.chat_model,
                                              s.tool_mode, s.embedding_model)
            self._provider_key = key
        return self._provider

    @property
    def embedder(self) -> Embedder:
        if self._embedder_override is not None:
            return self._embedder_override
        s = self.settings
        key = (s.embedding_provider, s.embedding_model, s.embedding_batch_size, id(self.provider))
        if self._embedder is None or self._embedder_key != key:
            self._embedder = build_embedder(s, self.provider)
            self._embedder_key = key
        return self._embedder

    def active_jobs(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM ingestion_jobs WHERE status='running'", (), 0)) + \
            int(self.db.scalar("SELECT COUNT(*) FROM tasks WHERE status IN ('planning','running','awaiting_approval')", (), 0))

    def hardware(self) -> dict:
        return hardware.snapshot(self.settings.data_dir, self.active_jobs())

    def global_stop(self) -> None:
        self.stop_event.set()
        self.db.audit("user", "global_stop", {})
        self.bus.publish("system.stop", {})

    def clear_stop(self) -> None:
        self.stop_event.clear()
        self.db.audit("user", "global_stop_cleared", {})
        self.bus.publish("system.resume", {})
