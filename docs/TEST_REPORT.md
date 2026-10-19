# Test report

Results below were produced in the build environment (Linux, Python 3.13, Node 22, 4 CPU cores, 16 GB RAM, **no GPU**, outbound network limited to the Python package index). Nothing here is estimated or assumed; items that could not be exercised are listed under **Not run**.

## Summary

| Suite | Command | Result |
|---|---|---|
| Backend (pytest) | `cd backend && python -m pytest tests -v` | **101 passed**, 0 failed, 0 skipped (~95 s) |
| Frontend unit tests (vitest) | `cd frontend && npm test` | **11 passed** |
| Frontend type-check + production build | `npm run build` | passed |
| Browser end-to-end (Playwright + Chromium) | `cd backend && python tests/e2e_browser_smoke.py` | **passed**: dashboard, document upload, grounded streaming chat with visible `[S1]` citation, all 10 pages, setup wizard (>=10/15 checks), zero console errors |
| Linux start/stop scripts | `scripts/setup.sh`, `start.sh`, `stop.sh` | exercised; found and fixed a PID-tracking bug |

Important context for reading these results:
* **LM Studio was simulated.** `backend/tests/mock_lmstudio.py` is a real HTTP server speaking LM Studio's protocol (OpenAI-compatible chat with SSE streaming, `/v1/models`, `/api/v0/models`, `/v1/embeddings`, load/unload, auth and error shapes). It exercises the real `LMStudioProvider` code but is **not a language model**. Agent-loop tests use a deterministic scripted model. These tests prove the integration and orchestration logic; they say nothing about how well a real model answers.
* The sandbox **blocked Hugging Face and public websites**, so web/dataset flows were tested against local HTTP servers, and Hugging Face against a fake API object.

## Your 23 acceptance workflows

| # | Workflow | Evidence (test) | Status |
|---|---|---|---|
| 1 | Connect to LM Studio and generate a response | `test_connection_and_generation`, `test_model_endpoints_and_failure_messages` (mock server) | Passed vs mock · **real LM Studio: not run** |
| 2 | Discover and select a model | `test_model_discovery_and_selection`, `test_streaming_and_load_unload`, API model endpoints | Passed vs mock |
| 3 | Multi-turn conversation | `test_multi_turn_persistent_chat_and_restart` (history reaches the model on turn 3) | Passed |
| 4 | Restart and retrieve saved memory | same test (new `AppContext` on same data dir); `test_remember_command_and_preference_in_prompt`; `test_setup_wizard_full_run_and_restart_persistence` | Passed |
| 5 | Import a PDF and answer from it with sources | `test_pdf_import_and_source_grounded_retrieval`, `test_pdf_upload_then_grounded_chat_with_citations`, browser E2E | Passed (answer text is mock-templated; retrieval, locator `page 2` and citation plumbing are real) |
| 6 | Multiple documents, each retrievable | `test_multiple_documents_each_retrievable` (DOCX, MD, code, HTML) | Passed |
| 7 | Small CSV with record counts | `test_csv_counts_malformed_and_duplicates` | Passed |
| 8 | Larger dataset in batches without loading it all in RAM | `test_large_dataset_processed_in_bounded_batches` (20 000 rows, every commit <= batch+12 chunks); `test_streaming_parser_uses_constant_memory_on_large_file` (1.2 M rows / >100 MB CSV, RSS growth asserted < 80 MB) | Passed |
| 9 | Resume an interrupted ingestion job | `test_resume_interrupted_job_matches_uninterrupted_run` (simulated hard crash mid-job, restart, identical result to uninterrupted run), `test_pause_resume_cancel_and_retry` | Passed |
| 10 | Detect duplicates | `test_duplicate_detection_exact_near_and_file` | Passed |
| 11 | Report malformed records and extraction errors | `test_csv_counts...`, `test_corrupt_unsupported_and_encoding_problems`, `test_archive_ingest_and_zip_slip_protection` | Passed |
| 12 | Import a public web page preserving the URL | `test_import_public_webpage_preserves_source_url` (URL + retrieval date kept) | Passed against a **local** server · **real public internet: not run** |
| 13 | Export structured results | `test_research_crawl_robots_dedup_blocks_and_export` (CSV/JSON/MD), collection export in API test, `report_generate` | Passed |
| 14 | Execute a permitted local operation | `test_permitted_local_operation_is_executed_and_verified` (write + SHA-256 re-read verification) | Passed |
| 15 | Require approval for a destructive action | `test_destructive_action_requires_approval_then_runs_after_user_approves`, `test_denied_approval_means_nothing_happens`, `test_agent_mode_over_api_with_approval_flow` | Passed |
| 16 | Cancel a running task | `test_cancel_running_task_while_waiting`, `test_cancel_kills_running_sandbox_and_global_stop`, `test_global_stop_button_cancels_everything` | Passed |
| 17 | Record feedback and retrieve a corrected preference | `test_correction_becomes_preference_and_is_applied_later` (new conversation follows it; regression case auto-created and passing), ambiguity/fact/one-time/hypothesis tests | Passed |
| 18 | Generate a candidate reusable workflow and evaluate it | `test_task_outcome_creates_episode_and_workflow_candidate_that_can_be_evaluated_and_approved` (candidate -> read-only replay -> human approval -> run on new input -> discoverable) | Passed |
| 19 | Detect a malicious instruction in a test webpage | `test_malicious_web_page_is_flagged_and_treated_as_data`, `test_malicious_page_is_quarantined_not_indexed` | Passed |
| 20 | Failed model connection gives a useful error | `test_failed_connection_gives_useful_error`, `test_unavailable_model_and_auth_errors`, `test_chat_error_event_when_server_down`, API test | Passed |
| 21 | Weights not represented as changed when only memory changed | `test_three_learning_systems_are_reported_separately_and_weights_untouched` | Passed |
| 22 | Fine-tuning workflow with a small compatible configuration | `test_real_tiny_cpu_finetune_end_to_end`: real LoRA (PEFT + Transformers + PyTorch, CPU) on a **tiny random GPT-2 built locally**; approval-gated start, baseline-vs-tuned evaluation (held-out loss 5.20 -> 4.91 in one run), adapter written, base-model files byte-identical | Passed (pipeline validated; says nothing about real-model quality). Needs the optional training libraries - skipped automatically if absent |
| 23 | Unsuccessful update can be rejected and rolled back | `test_unsuccessful_update_can_be_rejected_and_rolled_back_base_untouched`, `test_reject_registers_rejected_and_never_touches_settings` | Passed - **uses injected metrics** to drive the decision/registry/rollback state machine; a genuinely regressed real training run was not produced |

## Additional coverage

Permission engine (modes, fail-safe on unknown mode, path traversal/symlink/protected files, command allow-list and argument injection, authorized targets, approval binding and single use, the agent cannot approve itself), sandbox escape attempts and resource limits (CPU loop, memory bomb), SSRF/allow/deny policy, robots.txt, CAPTCHA/403 handled as "blocked" without bypass, resumable downloads and checksum verification, Hugging Face wrapper (gating, licence, splits, checksum), vector backends (SQLite and ChromaDB parametrized), embedder-mismatch degradation, quality gate, embedding outage pauses instead of failing, native tool-call path, malformed model output repair, step limits and repeated-failure blocking, task recovery after restart, extension proposal workflow, regression suite catching a bad "improvement", pcap/log/config/code analysis, origin guard and settings validation, hash-chained audit, backup/trash restore.

## Bugs found by testing (and fixed)

Race that let two workers index the same chunk (duplicate check moved inside the write transaction) - similarity floor missing (unrelated text returned) - sandbox-allow-list semantics - `db_query` authorizer ordering - tool "denied" state mis-labelled "failed" - learning hook racing the task's terminal status - FastAPI turning an absent boolean form field into `false` (upload did not wait) - **front-end upload bug** (live `FileList` cleared before use, found only by the browser run) - LM Studio's `embeddings` model type not normalised - **`start.sh` PID tracking** - invalid `execution_mode` falling through to the most permissive branch.

## Not run / not verified

* **A real LM Studio server and real models** (answer quality, real tool-call behaviour of any model, real embedding quality, real load/unload endpoints).
* **Windows**: the PowerShell scripts, the Windows installer steps, ChromaDB/PyMuPDF wheel availability on your Python, Windows firewall behaviour. (Linux equivalents of the scripts were run.)
* **Internet**: real Hugging Face downloads (including gated datasets), real public web pages, SearxNG/Brave search providers, Playwright JavaScript rendering.
* **GPU / QLoRA / bitsandbytes**, large-model fine-tuning, the merge -> GGUF -> quantize -> `lms import` export commands (llama.cpp not available).
* **Scale**: an indexed import of millions of records (largest indexed run: 20 000 rows; largest streamed: 1.2 M rows, profile-only).
* Cross-browser testing (only Chromium), screen-reader/accessibility audit, long-running soak tests, security penetration testing.
* `sentence-transformers` embedding provider (code present, not exercised).

## Reproduce

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-tests.ps1       # backend + frontend
cd backend; ..\.venv\Scripts\python.exe tests\e2e_browser_smoke.py    # optional browser E2E (needs playwright + Chromium)
```
