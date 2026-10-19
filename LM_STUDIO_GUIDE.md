# LM Studio guide

NEXUS uses **LM Studio** as its inference engine through LM Studio's local, OpenAI-compatible server. This guide covers installing LM Studio,
choosing models, and what NEXUS expects. (Official docs: https://lmstudio.ai/docs)

> **Honesty note:** the NEXUS test-suite exercises the LM Studio integration against a protocol-level mock server (same endpoints, streaming format and
> error shapes), not against a real LM Studio instance - none was available in the build environment. The first real-model run on your machine is
> the final proof; the Setup wizard steps 4-12 are designed to show you immediately if anything differs.

## 1. Install and download models

1. Install LM Studio from https://lmstudio.ai/ and start it.
2. **Discover** tab > search and download:
   * **A chat model** (instruct/chat tuned). Rough sizing for Q4 quantization:
     | Your hardware | Try |
     |---|---|
     | CPU only / 8 GB RAM | a 3B-4B instruct model |
     | 16 GB RAM or 6-8 GB VRAM | a 7B-8B instruct model (Q4_K_M) |
     | 12-24 GB VRAM | 12B-14B, or a 7-8B at higher quality (Q6/Q8) |
     Models marked with the **tool use** (hammer) badge support native function calling, which makes the agent more reliable. Others still work through NEXUS's validated JSON-action fallback.
   * **An embedding model** for true semantic search, e.g. a *nomic-embed-text* or *bge* model. Without it NEXUS uses a built-in lexical embedder that matches shared words, not meaning.
3. Wait for the downloads to finish (see the Downloads panel).

## 2. Load a model and start the server

1. **Developer** tab (the `>_` icon) > pick your chat model in the model selector > **Load**.
   * Set **Context Length** (e.g. 8192 or more if your RAM/VRAM allows). A longer context uses more memory. Remember this number: put it in NEXUS under *Models & learning > Generation settings > Prompt budget* so NEXUS never sends more than the model can hold.
2. Toggle **Start Server** (default `http://localhost:1234`). Leave "Serve on Local Network" **off**.
3. Optional: enable **Just-in-Time model loading** so NEXUS can use a downloaded-but-unloaded model; the first request will then be slow.
4. Optional: if you turned on API authentication, copy the key to `.env` as `NEXUS_LMSTUDIO_API_KEY` (or *Settings > Model endpoint*).

Verify in PowerShell:

```powershell
Invoke-RestMethod http://localhost:1234/v1/models | ConvertTo-Json -Depth 4
```

You should see your model ids. If this fails, NEXUS cannot connect either - see the error shown in the NEXUS header.

## 3. What NEXUS does with LM Studio

| Feature | How |
|---|---|
| Connection test | `GET /v1/models`; clear messages for "server not running", "bad API key", "model not found". |
| Model discovery | LM Studio's richer `GET /api/v0/models` (state, context length, quantization, capabilities) with automatic fallback to `/v1/models`. |
| Model selection | *Models & learning > LM Studio & models*. Blank = the first loaded chat model. |
| Streaming chat | `POST /v1/chat/completions` with `stream: true` (server-sent events). Reasoning models' `<think>...</think>` blocks are hidden. |
| Inference parameters | temperature, top_p, max_tokens, stop, seed, penalties, response_format - only parameters on a safe list are forwarded. |
| Embeddings | `POST /v1/embeddings` with the embedding model you choose. |
| Load / unload | LM Studio's REST `models/load` and `models/unload` endpoints when your version provides them; otherwise NEXUS tells you to use the app or the `lms` CLI (`lms load <model>`, `lms unload`, `lms ls`). |
| Tool calling | If the model advertises `tool_use`, or the probe succeeds, native tool calls are used. Otherwise NEXUS asks for one strict JSON action per turn, validates it against the tool's schema and the permission engine, and only then executes. You can force a mode in Settings (`tool_mode`). |
| Capabilities panel | Shown in the header and the Models page: native tools / JSON fallback, vision, context sizes, and limitations (e.g. "model downloaded but not loaded"). |

## 4. Choosing the embedding model (important)

Embeddings are tied to the model that made them. If you change the embedding model, existing collections keep working with **keyword** search, and the UI shows **needs re-index** - press *Knowledge > Collections > Re-index* to rebuild the vectors with the new model. NEXUS never mixes vectors from different embedders.

## 5. Using your own fine-tuned model in LM Studio

LM Studio loads **GGUF** files. A fine-tuned Transformers checkpoint is *not* directly loadable. NEXUS's optional fine-tuning module produces a LoRA adapter and documents the export: merge -> convert to GGUF with llama.cpp -> quantize -> `lms import`. See the *Fine-tuning* tab and the README section "Optional fine-tuning".

## 6. Troubleshooting LM Studio problems

| Message in NEXUS | Meaning / fix |
|---|---|
| "Cannot reach LM Studio at http://localhost:1234/v1" | Server not started, wrong port, or firewall. Start the server in the Developer tab; check the port. |
| "LM Studio rejected the API key" | Authentication is on. Set `NEXUS_LMSTUDIO_API_KEY`. |
| "Model '...' is not available" | The model id is wrong or not loaded and JIT loading is off. Load it or pick another in *Models*. |
| Answers are cut off / "context length" errors | Prompt budget in NEXUS is larger than the context you loaded. Lower it or reload the model with a bigger context. |
| Very slow first answer | The model is loading (JIT) or too large for your hardware. Use a smaller/more quantized model. |
| Agent produces invalid actions | Small models struggle with tool use. Use a tool-capable 7B+ instruct model or keep tasks simple. |
