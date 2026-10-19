# Architecture

NEXUS AI is a **modular monolith**: one Python process (FastAPI) containing independent modules, plus a separate React/TypeScript frontend that talks to it over REST and server-sent events (SSE). Everything runs locally; LM Studio is the only inference engine.

```
                      Browser (React + TypeScript + Tailwind)  ── REST + SSE ──┐
                                                                                ▼
 ┌──────────────────────────────────── FastAPI (nexus.api) ─────────────────────────────────────────────┐
 │  routes_system  routes_chat  routes_data  routes_agent   · origin guard · error mapping · SSE events   │
 ├───────────────┬───────────────┬──────────────┬─────────────┬───────────────┬────────────────────────┤
 │ agent         │ ingestion     │ knowledge    │ memory      │ tasks         │ training (optional)     │
 │  chat         │  parsers      │  store       │  store      │  orchestrator │  manager / runner       │
 │  actions      │  quality      │  vectorstore │  learning   │               │  (subprocess)           │
 │  prompts      │  pipeline     │  text utils  │             │               │                         │
 ├───────────────┴───────┬───────┴──────────────┴─────────────┴───────┬───────┴────────────────────────┤
 │ tools (registry,      │ permissions (policy, approvals, paths)     │ skills (workflows, extensions)  │
 │  executor, builtin)   │ web_research (fetcher, research, search)   │ evaluation (regression suite)   │
 │ datasets (HF/URL/local)│ system (hardware)                         │ events (bus)                    │
 ├───────────────────────┴────────────────────────────────────────────┴─────────────────────────────────┤
 │ llm: LLMProvider interface ── LMStudioProvider (OpenAI-compatible + /api/v0 + load/unload)             │
 │      Embedder interface ── Hash (offline) · LMStudio · sentence-transformers                           │
 ├─────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ database: SQLite (WAL) + FTS5          VectorStore interface ── ChromaDB · SQLite brute-force          │
 └─────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

`nexus.core.AppContext` builds and wires everything; tests construct one per test with fakes.

## Key design decisions

| Concern | Decision | Why |
|---|---|---|
| LLM provider | `LLMProvider` ABC (health, list_models, chat, stream_chat, embed, capabilities, load/unload). The agent never imports LM Studio code. | Swap providers without touching the agent. |
| Tool calling | `ActionProtocol` accepts native tool calls **or** one strict JSON action per turn, with repair prompts; both end in the same validated `Action`. | Not every model supports function calling. |
| Structured data | SQLite for records/metadata/memory/audit; vectors only for text chunks and memories; tabular rows are indexed as grouped chunks **and** profiled (schema, null rates, ranges) in `dataset_profiles`. | "Don't force everything into a vector DB." |
| Retrieval | Hybrid: vector similarity + FTS5/BM25 fused with reciprocal-rank fusion, a similarity floor so unrelated text is not returned, and a small learned *utility* factor from feedback. | Robust when the embedder is lexical, unavailable or changed. |
| Embedding consistency | Each collection records the embedder id; mismatch -> keyword-only retrieval + "needs re-index" flag. Vectors from different models are never mixed. | Correctness over silent degradation. |
| Ingestion durability | Vectors are upserted first (idempotent, deterministic ids); then one SQLite transaction writes chunks + FTS rows + facts + rejects **and** the file checkpoint. Duplicate checks are repeated inside that transaction. | Crash-safe resume; no race duplicates between workers. |
| Context hygiene | Separate contexts: conversation (rolling summary + last N messages + retrieval), task (checkpointed history), knowledge (retrieved on demand). Token budget derived from the configured context window. | One task never contaminates another; no full-history resends. |
| Postgres upgrade path | All SQL lives in `database/db.py` and the stores; FTS5 -> tsvector, `INSERT OR IGNORE` -> `ON CONFLICT DO NOTHING`, SQLite vectors -> pgvector. Not implemented in v1. | Documented path, honestly deferred. |

## Data model (SQLite)

`conversations`, `messages`, `collections`, `documents`, `chunks` (+`chunks_fts`), `vectors`, `facts`, `memories` (+`memories_fts`), `ingestion_jobs/items/rejects`, `dataset_profiles`, `downloads`, `research_jobs/pages`, `tasks`, `task_events`, `tool_runs`, `approvals`, `audit_log` (hash-chained), `skills`, `feedback`, `eval_cases/runs`, `training_runs`, `model_registry`, `projects`, `meta`.

## Memory layers

| Layer | Where | Notes |
|---|---|---|
| Working | `conversations.state`, `tasks.state` | current conversation/task, referents ("that document"), checkpointed history |
| Episodic | `memories` (layer `episodic`) | task outcomes (system-written, *unverified*), corrections, follow-ups |
| Semantic | `memories` (`semantic`) + `facts` | facts with trust, confidence, timestamps and source references |
| Procedural | `memories` (`procedural`) + `skills` | lessons; workflows (approved only are used) |
| Preferences | `memories` (`preference`) | injected into every system prompt |
| Project | `memories.project_id`, `conversations.project_id` | scoping so projects do not leak |

Trust levels: `user_verified` > `source_backed` > `unverified` > `hypothesis`. Only a human action produces `user_verified`.

## Agent loop

interpret -> retrieve memory/skills -> plan (may ask clarifying questions) -> permission check -> **one** bounded action -> inspect (untrusted outputs fenced and scanned) -> retry/adapt with per-call failure limits -> validate final report (flags unmentioned failures) -> record episode -> maybe propose a workflow candidate. Limits: steps, tool calls, wall-clock, retries, concurrency. States: pending, planning, running, awaiting_approval, awaiting_input (extra), completed, failed, cancelled. Each tool call is stored as `intended -> attempted -> completed/verified` (or denied/failed/cancelled).

## Three learning systems (never conflated)

1. **Ingestion** (`ingestion`, `datasets`, `web_research`) adds searchable text.
2. **Persistent learning** (`memory`, `skills`, `evaluation`) remembers, learns workflows from verified outcomes and guards them with a regression suite.
3. **Weight adaptation** (`training`) - optional, approval-gated, separate status, logs and evaluation. `learning.overview()` reports all three; `weights_modified` is true only if an accepted adapter is active.
