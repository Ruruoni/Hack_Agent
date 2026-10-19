# LM Studio guide

NEXUS uses **LM Studio** as its inference engine through LM Studio's local, OpenAI-compatible server. This guide covers installing LM Studio, loading a chat model, finding its identifier, connecting NEXUS, verifying a *real* response, and setting up an embedding model for semantic search. (Official docs: https://lmstudio.ai/docs)

> **Verification status:** the NEXUS test-suite exercises this integration against a protocol-level **mock** of LM Studio (same endpoints, streaming format and error shapes), not a real LM Studio - none was available in the build environment. Steps 3-5 below are exactly how *you* turn that into proof on your machine. LM Studio's menus and API details change between versions; where this guide is unsure it says so.

## 1. Install and download models

1. Install LM Studio from https://lmstudio.ai/ and start it.
2. **Discover** tab > search and download:
   * **A chat model** (instruct/chat tuned). Rough sizing for Q4 quantization:

     | Your hardware | Try |
     |---|---|
     | CPU only / 8 GB RAM | a 3B-4B instruct model |
     | 16 GB RAM or 6-8 GB VRAM | a 7B-8B instruct model (Q4_K_M) |
     | 12-24 GB VRAM | 12B-14B, or a 7-8B at higher quality (Q6/Q8) |

     Models marked with the **tool use** badge support native function calling, which makes the agent more reliable. Others still work through NEXUS's validated JSON-action fallback (less reliable on small models).
   * **An embedding model** (see section 4), e.g. a *nomic-embed-text* or *bge* model.
3. Wait for the downloads to finish (Downloads panel). A model larger than your free RAM/VRAM will be extremely slow or fail to load - pick a smaller one.

## 2. Load the chat model and start the server

1. **Developer** tab (the `>_` icon) > select your chat model > **Load**.
   * Set **Context Length** (e.g. 8192; more needs more memory). Remember the number - NEXUS needs it (section 3, step 4).
2. Switch on **Start Server** (default `http://localhost:1234`). Leave "Serve on Local Network" **off**.
3. Optional: **Just-in-Time model loading** lets NEXUS use a downloaded-but-unloaded model; the first request is then slow.
4. Optional: if you enabled API authentication, put the key in `.env` as `NEXUS_LMSTUDIO_API_KEY=...` (or *Settings > Model endpoint*).

## 3. Find the model identifier, configure NEXUS, verify a real response

**Step 1 - list the identifiers LM Studio exposes** (PowerShell):

```powershell
(Invoke-RestMethod http://localhost:1234/v1/models).data | ForEach-Object { $_.id }
```

Each line is a model **identifier** (for example something like `qwen2.5-7b-instruct`; yours will differ). The same ids appear in the LM Studio Developer tab, and `lms ls` lists downloaded models if you installed the `lms` CLI. If the command errors, the server is not running (section 2). If your chat model is missing, load it (or enable JIT loading).

**Step 2 - send a real request straight to LM Studio** (bypasses NEXUS; replace `YOUR-MODEL-ID`):

```powershell
$body = @{ model = "YOUR-MODEL-ID"; messages = @(@{ role = "user"; content = "Reply with exactly: NEXUS test OK" }); max_tokens = 40; temperature = 0 } | ConvertTo-Json -Depth 5
(Invoke-RestMethod -Uri http://localhost:1234/v1/chat/completions -Method Post -ContentType "application/json" -Body $body).choices[0].message.content
```

A sensible reply (ideally `NEXUS test OK`) proves LM Studio itself works. (These snippets were run against the project's mock server under PowerShell Core; against real LM Studio they are **yours to run**.)

**Step 3 - tell NEXUS which model to use.** Either:
* in the web UI: **Models & learning > LM Studio & models** > *Use for chat* on that row; or
* in `.env`: `NEXUS_CHAT_MODEL=YOUR-MODEL-ID` (then restart). Leave it empty to use whichever chat model is loaded.

Precedence: a value saved from the UI or the Setup wizard (`data\settings.json`) **overrides** `.env`. If NEXUS uses a model you did not expect, check the UI.

**Step 4 - set the prompt budget.** *Models & learning > Generation settings > Prompt budget* = the Context Length you loaded (step 2.1). If it is larger than the real context, long conversations fail or get truncated; smaller is safe.

**Step 5 - verify through NEXUS.**
1. Top bar: *LM Studio connected* and your model id. If it says *offline*, the red box shows the exact reason and fix.
2. **Models & learning > LM Studio & models > Test generation** - shows the reply, the model id and the seconds taken.
3. **Chat** > ask a simple question - the answer streams in. The header badge shows **native tools** or **JSON-action fallback** (see section 6).
4. Optional: **Probe tool calling** tests whether this model really supports native tool calls.

A reply that begins with `Mock reply to:` means you are talking to the test mock, not LM Studio (only happens if you ran the test suite's mock).

## 4. Configure and test an embedding model

An embedding model turns text into vectors so searches match **meaning**, not just shared words. Without one NEXUS uses a built-in lexical embedder.

1. In LM Studio **Discover**, download an embedding model (e.g. *nomic-embed-text*). It does not need to be "loaded" like a chat model if JIT loading is on; otherwise load it in the Developer tab (several models can be loaded together if memory allows).
2. Find its id with the `/v1/models` command from section 3 - embedding models appear in the same list.
3. Test it directly (replace `YOUR-EMBEDDING-ID`):

```powershell
$e = @{ model = "YOUR-EMBEDDING-ID"; input = @("hello world") } | ConvertTo-Json
(Invoke-RestMethod -Uri http://localhost:1234/v1/embeddings -Method Post -ContentType "application/json" -Body $e).data[0].embedding.Count
```

A number such as 384, 768 or 1024 (the vector size) means it works. An error means the model is not available to the server.
4. Tell NEXUS: **Models & learning > Embedding model**: *Provider* = **LM Studio embedding model**, *Model id* = your embedding id > **Save embedder**. (`.env` equivalent: `NEXUS_EMBEDDING_PROVIDER=lmstudio` and `NEXUS_EMBEDDING_MODEL=<id>`; UI-saved values win.) The panel must show **working (dim N)** and no *lexical fallback* badge.
5. Re-index what you already ingested (next section).

## 5. Lexical search vs real semantic search, and re-indexing

| | Built-in embedder (default) | LM Studio embedding model |
|---|---|---|
| Matches | shared words and word-parts (a hashed bag of words) | meaning, paraphrases, synonyms |
| Needs | nothing, works offline | a downloaded embedding model and the server running |
| Weakness | paraphrases with no shared words | slower ingestion (a model call per batch); quality depends on the model |
| UI label | *lexical* | *semantic* |

NEXUS always combines vector similarity with keyword (BM25) search, so exact terms keep working either way.

**Measured example of the lexical weakness** (sample files, built-in embedder): `Which extension does the Orion support desk use?` finds the right PDF page first (shared words), but `how long do we keep backups` ranks a `tickets.csv` chunk above the PDF page that answers it. A semantic embedder is expected to rank the PDF page first; **that improvement was not measured here** (no real embedding model was available) - Part H of [docs/ACCEPTANCE_TEST.md](docs/ACCEPTANCE_TEST.md) lets you measure it yourself.

**Vectors are tied to the model that made them.** After you change the embedding model (or the vector store backend):
1. Collections built with the old model show **needs re-index** and, until fixed, searches use keyword matching only and say so ("keyword search only. Re-index to fix").
2. **Knowledge & memory > Collections > Re-index** on each collection (re-embeds every chunk; originals untouched; the speed depends on your model and chunk count).
3. **Knowledge & memory > Memory > Re-index memories** - memories have their own vectors and are *not* flagged automatically.
4. Check: **Models & learning** shows no *needs re-index* warning and a search returns results without the "keyword search only" notice.

(This sequence - lexical -> LM Studio embeddings -> *needs re-index* flag -> keyword-only notice -> re-index -> clean search - was executed against the mock embeddings endpoint. Embedding *quality* is unverified.)

If the embedding server is unavailable during ingestion, the job **pauses** with "Embedding model unavailable" and keeps its checkpoint; fix LM Studio and press Resume. If the embedder fails during a Chat question, NEXUS falls back to keyword search for that question and tells you.

## 6. What NEXUS does with LM Studio

| Feature | How |
|---|---|
| Connection test | `GET /v1/models`; clear messages for "server not running", "bad API key", "model not found". |
| Model discovery | LM Studio's richer `GET /api/v0/models` (state, context length, quantization, capabilities) with automatic fallback to `/v1/models`. Embedding models are recognised even when reported as type `embeddings`. |
| Streaming chat | `POST /v1/chat/completions` with `stream: true` (server-sent events). Reasoning models' `<think>...</think>` blocks are hidden. |
| Inference parameters | temperature, top_p, max_tokens, stop, seed, penalties, response_format - only a safe list is forwarded. |
| Embeddings | `POST /v1/embeddings` with the embedding model you choose. |
| Load / unload | LM Studio's REST `models/load` and `models/unload` when your version has them (**unverified against real LM Studio**); otherwise NEXUS tells you to use the app or the CLI (`lms load <model>`, `lms unload`, `lms ls`). |
| Tool calling | Native tool calls if the model advertises `tool_use` or the probe succeeds; otherwise one strict JSON action per turn, validated against the tool schema and the permission engine before anything runs. Force a mode with `tool_mode` in Settings. |
| Capabilities panel | Header and Models page: native tools / JSON fallback, vision, context sizes, limitations. |

## 7. Using your own fine-tuned model in LM Studio

LM Studio loads **GGUF** files. A fine-tuned Transformers checkpoint is *not* directly loadable. NEXUS's optional fine-tuning module produces a LoRA adapter and documents the export: merge -> convert to GGUF with llama.cpp -> quantize -> `lms import`. See *Models & learning > Fine-tuning* and the README.

## 8. Troubleshooting LM Studio problems

| Message in NEXUS | Meaning / fix |
|---|---|
| "Cannot reach LM Studio at http://localhost:1234/v1" | Server not started, wrong port, or firewall. Start the server in the Developer tab; try `http://127.0.0.1:1234/v1` in *Settings > Model endpoint*. |
| "LM Studio rejected the API key" | Authentication is on. Set `NEXUS_LMSTUDIO_API_KEY`. |
| "Model '...' is not available" | The id is wrong, or the model is not loaded and JIT loading is off. Re-run the `/v1/models` command, load it, and re-select it in NEXUS. |
| Chat model list is empty | Nothing downloaded/loaded. Download one in Discover and load it. |
| Answers cut off / "context length" errors | NEXUS's prompt budget is larger than the context you loaded. Lower it or reload the model with a bigger context. |
| Very slow first answer | The model is loading (JIT) or too large for your hardware (see docs/TROUBLESHOOTING.md > Memory). |
| Agent tasks fail with "could not produce a valid action" | The model cannot follow the JSON-action protocol. Use a larger tool-capable instruct model or keep tasks simple. |
| Embedding errors | See docs/TROUBLESHOOTING.md > Embeddings. |
