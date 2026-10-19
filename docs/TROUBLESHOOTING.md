# Troubleshooting

Start with **Setup wizard** (web UI) or `scripts\doctor.ps1`: they test each layer and print a hint. Logs: `data\logs\backend.log` and `backend.err.log`.

## Installation

| Symptom | Cause / fix |
|---|---|
| `python` is "not recognized" | Python is not on PATH. Re-run the Python installer > *Modify* > tick *Add Python to environment variables*; open a **new** PowerShell. |
| `running scripts is disabled on this system` | Use `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` (this affects only that command). |
| `pip install` fails building `chroma-hnswlib` / "Microsoft Visual C++ 14.0 required" | Optional package. NEXUS works without ChromaDB (built-in SQLite vector store). To get it, install *Microsoft C++ Build Tools* (https://visualstudio.microsoft.com/visual-cpp-build-tools/) or use Python 3.12. |
| `pip` cannot find a wheel for PyMuPDF / pyarrow / numpy | You are on a very new or 32-bit Python. Install 64-bit Python 3.12. |
| `npm` not found | Install Node.js LTS and open a **new** terminal. The API works without it; only the web UI build needs it. |
| `npm install` is slow / fails behind a proxy | Set `npm config set proxy http://host:port` (and `https-proxy`). |
| Setup finished but `scripts\start.ps1` says "did not start" | Open `data\logs\backend.err.log`. Common: port 8000 taken (`scripts\start.ps1 -Port 8010`), or a package failed to import. |

## Connection

| Symptom | Fix |
|---|---|
| Header says **LM Studio offline** | In LM Studio > Developer: load a model and **Start Server**. Check the URL in *Settings > Model endpoint* (default `http://localhost:1234/v1`). |
| Works in browser but NEXUS says unreachable | Windows firewall or VPN. Try `http://127.0.0.1:1234/v1` instead of `localhost`. |
| "rejected the API key" | Set `NEXUS_LMSTUDIO_API_KEY` in `.env` or Settings. |
| Browser shows "Cannot reach the NEXUS backend" | The backend is not running: `scripts\start.ps1`. |
| API calls from another site are rejected (`origin_blocked`) | By design. Only `localhost:5173/8000` origins may call the API; add yours in *Settings > Privacy* if you build your own UI. |

## Retrieval / knowledge

| Symptom | Fix |
|---|---|
| Search finds nothing | Check *Knowledge > Collections* shows chunks > 0. Unrelated text is intentionally not returned. Try different words (the built-in embedder is lexical). |
| Collection shows **needs re-index** | The embedding model changed. Press *Re-index*. Until then only keyword search works. |
| Answers ignore my documents | Make sure *Use my knowledge* is ticked and the right collection is selected; look at the sources panel under the answer. |
| PDF shows "page has no extractable text" | The PDF is a scan. OCR is not built in; run OCR first (e.g. in Acrobat/ocrmypdf). |
| Ingestion job **paused: Quality gate** | Too many rejected/quarantined chunks. Open *Rejected records*, fix the filter or the source, then Resume (or "Resume anyway"). |
| Job **paused: Embedding model unavailable** | LM Studio stopped or the embedding model is not loaded. Fix it and Resume - the checkpoint is kept. |
| Job **interrupted** after a crash/restart | Press Resume. It continues from the last committed batch. |
| Disk full error | Free space; the job refuses to start with less than *Keep this much disk free* (default 500 MB) after the estimated need. |

## Agent

| Symptom | Fix |
|---|---|
| Task stuck on **awaiting approval** | Open *Approvals* (top bar). The agent cannot approve itself. Approvals expire after the timeout (default 15 min). |
| "Permission denied: outside the allowed folders" | Add the folder in *Settings > Permissions > Extra folders*, or copy the file into `data\workspace`. |
| "Target ... is not in the authorized targets list" | Add the host/CIDR you are permitted to test under *Settings > Authorized targets*. |
| "The model could not produce a valid action" | The model cannot follow the JSON-action protocol. Use a larger instruct/tool-capable model, or lower task complexity. |
| Task failed: step/time limit | Raise limits in *Settings > Agent limits*, or split the objective. |
| After a restart tasks show failed "Interrupted by application restart" | Use **Resume from checkpoint** on the task. |

## Web research

| Symptom | Fix |
|---|---|
| Page shows `blocked: robots.txt disallows` / `login required` / `CAPTCHA` | NEXUS never bypasses these. Use another source or fetch the page manually and ingest the saved file. |
| `resolves to a private/loopback address` | SSRF protection. For a local lab site only, enable *Allow private hosts* in Settings. |
| "No search provider is configured" | Provide URLs directly, or configure SearxNG/Brave in *Settings > Web research & search*. |
| JavaScript page returns little text | Install Playwright: `pip install playwright` then `playwright install chromium`, and tick *Render JavaScript*. |

## Datasets / training

| Symptom | Fix |
|---|---|
| "gated dataset" | Accept its terms on huggingface.co, set `HF_TOKEN` in `.env`, restart. Never paste tokens in chat. |
| "approval required" for a download | Tick the explicit approval box after reading the size/disk plan. |
| Fine-tuning says "libraries missing" | `scripts\setup.ps1 -WithTraining` (install the right PyTorch build first from https://pytorch.org/get-started/locally/). |
| "model is a GGUF file" | GGUF cannot be fine-tuned. Use the original Hugging Face-format model folder. |
| "not feasible on this machine" | The memory estimate exceeds your GPU/RAM. Use a smaller model, QLoRA (NVIDIA GPU), a shorter sequence length - or keep using retrieval and memory. |

## Still stuck?

1. `scripts\doctor.ps1` and the Setup wizard output.
2. `data\logs\backend.err.log` (last 50 lines).
3. *Settings > Backup, audit & history* shows the audit trail of what the app did.
