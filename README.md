# NEXUS AI

A **locally hosted, general-purpose autonomous AI agent** powered by [LM Studio](https://lmstudio.ai/). It chats conversationally, ingests large collections of documents and datasets, keeps persistent memory, uses tools under strict permission control, researches the public web, learns reusable workflows from verified outcomes, and - optionally - can fine-tune model weights. Everything runs on your machine.

> **Three different kinds of "learning", never mixed up (and shown separately in the UI):**
> 1. **Data ingestion** - adds searchable documents/records. *Does not change the model.*
> 2. **Persistent learning** - remembers facts, preferences, corrections, task outcomes and approved workflows. *Does not change the model.*
> 3. **Model fine-tuning** - optional LoRA/QLoRA that really changes weights; separate, approval-gated, evaluated against the baseline, reversible.

## Quick start (Windows + VS Code)

Full beginner guide: **[INSTALL_WINDOWS.md](INSTALL_WINDOWS.md)**.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1     # once: Python env, packages, web UI build, database
# start LM Studio, load a model, Developer tab > Start Server  (see LM_STUDIO_GUIDE.md)
powershell -ExecutionPolicy Bypass -File scripts\start.ps1     # opens http://127.0.0.1:8000
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
```

Then open **Setup wizard** in the app: 15 checks that prove each part works on your PC.
Linux/macOS: `scripts/setup.sh`, `scripts/start.sh`, `scripts/stop.sh`.

## What is in the box

| Area | Capabilities |
|---|---|
| **Chat** | Streaming multi-turn chat, persistent history, titles, rolling summaries, reference resolution ("that document"), personality / length / beginner-vs-technical modes, clarifying questions, honest-uncertainty prompt rules, numbered **source citations**, file upload, thumbs and corrections, stop button, agent mode with live task progress and inline approvals. |
| **LM Studio** | Connection test, model discovery (rich `/api/v0/models` with fallback), selection, load/unload where supported, streaming, context budget, inference parameters, capability detection (native tools vs JSON-action fallback, vision, context), clear errors, provider adapter interface. |
| **Ingestion** | PDF, DOCX, TXT, MD, HTML, CSV/TSV, JSON/JSONL, XLSX, XML, Parquet, code, configs, logs, archives, URLs. Streaming, bounded workers, chunked transactional writes, **checkpoints, pause/resume/retry/cancel**, crash recovery, disk checks, exact + near-duplicate detection, quality filters, **quarantine** for injection-like text, quality gate, structured profiles, per-item processing records, batch reports with retrieval self-check. |
| **Datasets** | Hugging Face / URL / local: inspect, licence & access info, size and disk checks, explicit approval for big downloads, resumable verified downloads, schema preview, import. GUI and CLI. |
| **Memory** | SQLite + vector index. Working, episodic, semantic, procedural, preference and project memory with trust levels, confidence, timestamps, provenance; search, edit, verify, mark outdated, delete, export, re-index. |
| **Agent** | Bounded orchestrator (plan, one action at a time, retries, limits, checkpoints, resume, cancel, global STOP), intended/attempted/verified tracking, 27 tools (files, knowledge, memory, CSV/JSON/data analysis, code/config/log/pcap analysis, web search/fetch/scrape/research, sandboxed Python, registered commands, read-only SQL, reports, diagnostics, API integrations), extension proposals for missing capabilities. |
| **Safety** | Read-only / approval-required / restricted-automation modes, path policy, validated commands, sandbox, authorized-target scoping, approvals bound to exact arguments, audit log with hash chain, backups, recoverable trash. See [docs/SECURITY.md](docs/SECURITY.md). |
| **Web research** | Allow/deny lists, robots.txt, rate limits, SSRF guard, no bypass of logins/paywalls/CAPTCHAs, tables/headings/links/metadata, dedup, saved parameters, CSV/JSON/MD export, review-then-add-to-knowledge, resumable jobs. |
| **Adaptive learning** | Corrections classified as one-time / preference / fact / hypothesis (asks when ambiguous), regression cases auto-created, feedback adjusts retrieval ranking, task episodes recorded, workflow candidates replayed read-only and regression-tested, human approval required, version history. |
| **Fine-tuning (optional)** | Hardware & library check, model compatibility (rejects GGUF), dataset cleaning + licence confirmation + leak-free splits, resource estimate, approval-gated subprocess training (Transformers + PEFT), baseline-vs-tuned evaluation, accept/reject, activate/rollback, GGUF export workflow. |

## Using it

* **Chat** - upload a document, ask about it, expand the sources. Say *"remember that ..."* or correct an answer with **Correct this**.
* **Agent mode / Agent tasks** - describe a goal; watch the plan, tool calls, verification states; approve or deny consequential steps.
* **Data ingestion / Dataset manager** - [DATASET_GUIDE.md](DATASET_GUIDE.md).
* **Knowledge & memory** - inspect sources, edit/verify memories, re-index, export.
* **Models & learning** - LM Studio status, the three learning systems, regression suite, optional fine-tuning.
* **Web research**, **Skills & extensions**, **Settings** (permissions, limits, paths, search, privacy, backup, audit).

CLI: `python -m nexus.cli serve | init-db | doctor | ingest | dataset ... | backup | restore | test` (run from `backend/`).

## Project layout

```
backend/nexus/   llm  agent  memory  knowledge  ingestion  datasets  web_research  tools  skills
                 permissions  tasks  evaluation  training  database  api  system  (+ core.py, config.py, cli.py)
backend/tests/   automated tests (pytest) + a protocol-level mock of LM Studio
frontend/        React + TypeScript + Tailwind (Vite); vitest tests
scripts/         PowerShell (and .sh) setup / start / stop / tests / doctor / backup
docs/            ARCHITECTURE, SECURITY, TROUBLESHOOTING, TEST_REPORT
samples/         tiny demo document, CSV and fine-tuning dataset
data/            created at run time (database, vectors, raw inputs, reports, logs) - git-ignored
```

## Tests

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-tests.ps1
```

See **[docs/TEST_REPORT.md](docs/TEST_REPORT.md)** for what was verified, how, and - just as important - what was **not** run. In short: the backend suite (100 tests) and frontend suite pass in the build environment, including a real CPU LoRA run on a tiny locally-built model, but no real LM Studio, no Windows host and no internet access to Hugging Face / public sites were available there.

## Honest limitations and deferred work

* Tested against a protocol-level mock of LM Studio, not a real one (see test report). Real-model answer quality is unmeasured.
* The built-in embedder is **lexical**; for semantic search load an embedding model in LM Studio (or install sentence-transformers) and re-index.
* No OCR for scanned PDFs; no image/vision input; JavaScript page rendering needs optional Playwright (code path present, not exercised in tests).
* The Python sandbox is best-effort isolation, not a security boundary. There is no user authentication on the local API.
* PostgreSQL/pgvector and multi-user deployment are a documented upgrade path, not implemented.
* QLoRA/GPU training and the GGUF export commands are implemented but could not be run in the build environment (CPU-only LoRA was). Fine-tuning quality on real models is untested.
* Scale: streaming and checkpointing are designed for millions of records and verified on 1.2 M rows (profile-only) and 20 k indexed rows; a multi-million-row *indexed* import was not performed.
* PowerShell scripts were written carefully but not executed on Windows in the build environment (their Linux twins were).

## License

No license file is included; add one before redistributing.
