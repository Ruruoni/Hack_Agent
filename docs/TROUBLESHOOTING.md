# Troubleshooting

Start with **Setup wizard** (web UI) or `scripts\doctor.ps1`: they test each layer and print a hint. Logs: `data\logs\backend.log` and `backend.err.log`.

## Quick triage (do this first)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1                 # connection / model / database checks with hints
Get-Content data\logs\backend.err.log -Tail 40                            # the real error message
netstat -ano | findstr :8000                                              # is something listening on the NEXUS port?
```

## Installation

| Symptom | Cause / fix |
|---|---|
| `python` is "not recognized" | Python is not on PATH. Re-run the Python installer > *Modify* > tick *Add Python to environment variables*; open a **new** PowerShell. |
| `running scripts is disabled on this system` / `cannot be loaded because running scripts is disabled` / `UnauthorizedAccess` | Windows blocks `.ps1` files by default. Always start scripts as `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` (affects only that command, needs no administrator rights). To make plain `.\scripts\start.ps1` work: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. If the files came from a ZIP and Windows still refuses ("not digitally signed"/"from the internet"), run `Get-ChildItem -Recurse scripts | Unblock-File` once. A company-managed PC may forbid changing the policy: ask IT, or run the Python commands from the scripts manually. |
| `npm` fails with "running scripts is disabled" although you used `-ExecutionPolicy Bypass` | You ran `npm` yourself in a normal PowerShell: use `npm.cmd` instead (e.g. `npm.cmd run build`) or `cmd /c npm run build`. |
| `python` opens the Microsoft Store | Settings > Apps > Advanced app settings > *App execution aliases*: turn off the "python.exe" entries, or install Python from python.org and reopen PowerShell. |
| `ModuleNotFoundError: No module named ...` when starting | The packages are missing from `.venv` (setup was interrupted or you ran the system Python). Run `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` again; or `.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt`. Always use `.venv\Scripts\python.exe`, not a bare `python`. |
| `pip` stops with "Could not find a version that satisfies the requirement" | Old/32-bit/very new Python, or no internet/proxy. Use 64-bit Python 3.12; set `pip config set global.proxy http://host:port` if behind a proxy. |
| "No module named nexus" | You ran a command from the wrong folder. `nexus.cli` commands are run from the `backend` folder (`cd backend`); the scripts do that for you. |
| `Failed building wheel for pyyaml` / `Microsoft Visual C++ 14.0 or greater is required` during `pip install` | Your Python is too new (3.14/3.15 have no prebuilt wheels yet), so pip compiles from source. **Do not install the C++ tools; install Python 3.12** from https://www.python.org/downloads/windows/ (it can coexist with your other Python). Then, from the project folder: `Remove-Item -Recurse -Force .venv` and re-run `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` (it picks 3.12 through the `py` launcher; check with `py -3.12 --version`). |
| `pip install` fails building `chroma-hnswlib` / "Microsoft Visual C++ 14.0 required" | Optional package. NEXUS works without ChromaDB (built-in SQLite vector store). To get it, install *Microsoft C++ Build Tools* (https://visualstudio.microsoft.com/visual-cpp-build-tools/) or use Python 3.12. |
| `pip` cannot find a wheel for PyMuPDF / pyarrow / numpy | You are on a very new or 32-bit Python. Install 64-bit Python 3.12. |
| `npm run build` crashes with `FATAL ERROR: Zone Allocation failed - process out of memory` / "Last few GCs" | A Node.js bug/instability, most often on a very new non-LTS "Current" Node (odd version numbers such as 23/25). **Nothing is wrong with your RAM or the project.** Either (a) ignore it: since the fix, `setup.ps1` falls back to the **prebuilt UI** in `frontend\prebuilt`, and `start.ps1` serves it; or (b) install **Node 22 LTS** from https://nodejs.org/ (uninstall the newer one first), reopen PowerShell, run `node --version` (expect v22.x) and re-run setup to build a fresh UI. |
| `Cannot find module @rollup/rollup-win32-x64-msvc` (or `@esbuild/win32-x64`) during `npm run build` | npm's optional-dependency bug (a lockfile made on another OS). Current setup removes the lockfile and retries automatically; by hand: `cd frontend; Remove-Item -Recurse -Force node_modules; Remove-Item -Force package-lock.json -ErrorAction SilentlyContinue; npm install; npm run build`. The "esbuild ... install-scripts ... allowScripts" warning is harmless. Without a successful build NEXUS still runs with the prebuilt UI. |
| `npm` not found | Install Node.js LTS and open a **new** terminal. The API works without it; only the web UI build needs it. |
| `npm install` is slow / fails behind a proxy | Set `npm config set proxy http://host:port` (and `https-proxy`). |
| Setup finished but `scripts\start.ps1` says "did not start" | Open `data\logs\backend.err.log`. Common: port 8000 taken (`scripts\start.ps1 -Port 8010`), or a package failed to import. |

## Connection

| Symptom | Fix |
|---|---|
| Port **8000 is already in use** (`start.ps1` says so) | Find the owner: `netstat -ano \| findstr :8000` then `tasklist /FI "PID eq <pid>"`. Close that program, or start NEXUS elsewhere: `scripts\start.ps1 -Port 8010` and open http://127.0.0.1:8010. LM Studio's own port 1234 is separate; if *that* clashes, change it in LM Studio's Developer tab and in *Settings > Model endpoint*. |
| `start.ps1` says "already running" but the page does not load | A stale process: `scripts\stop.ps1`, then `start.ps1`. If still stuck, end `python.exe` in Task Manager (Details tab; the one using `.venv`). |
| Browser says `host_blocked` (403) | You opened NEXUS through a name other than `localhost`/`127.0.0.1` (e.g. your PC name or LAN IP). Use `http://127.0.0.1:8000`, or add the name in *Settings > Privacy > Allowed host names* (not recommended on shared networks). |
| Header says **LM Studio offline** | In LM Studio > Developer: load a model and **Start Server**. Check the URL in *Settings > Model endpoint* (default `http://localhost:1234/v1`). |
| Works in browser but NEXUS says unreachable | Windows firewall or VPN. Try `http://127.0.0.1:1234/v1` instead of `localhost`. |
| "rejected the API key" | Set `NEXUS_LMSTUDIO_API_KEY` in `.env` or Settings. |
| Browser shows "Cannot reach the NEXUS backend" | The backend is not running: `scripts\start.ps1`. |
| API calls from another site are rejected (`origin_blocked`) | By design. Only `localhost:5173/8000` origins may call the API; add yours in *Settings > Privacy* if you build your own UI. |

## Memory (RAM / disk) problems

| Symptom | Fix |
|---|---|
| LM Studio says it cannot load the model / the PC freezes when the model loads | The model (plus context) does not fit in free RAM/VRAM. Close other programs, pick a smaller or more quantized model (3-4B or Q4), lower Context Length in LM Studio. Rule of thumb: Q4 needs roughly 0.6 GB per billion parameters plus context. |
| Very slow generation (seconds per word) | Running on CPU or swapping to disk. Smaller model; make sure GPU offload is enabled in LM Studio. |
| Ingestion is slow or the PC lags | *Data ingestion > New job > Filtering and quality rules*: lower **Parallel workers** to 1 and **batch size** to 16; ingest folders in smaller pieces. A real embedding model also slows ingestion (one model call per batch). |
| `MemoryError` / backend killed while ingesting a huge file | Streaming keeps RAM low for CSV/JSONL/Parquet/PDF, but a single huge JSON *object* or DOCX loads fully: convert JSON to JSONL; split the document. |
| Fine-tuning refused as "not feasible" | By design (the estimate exceeds your GPU/RAM). Use a smaller model or skip fine-tuning; retrieval and memory still work. |
| "Not enough free disk space" | Free space: a job needs roughly the input size (raw copy is skipped for files already under `data\`) plus the index, plus the 500 MB reserve (*Settings > Ingestion & storage*). |

## Embeddings (failed or unavailable)

| Symptom | Fix |
|---|---|
| Models page: embedding *Error* / red message | LM Studio is off, the embedding model id is wrong, or the model is not available. Re-run the `/v1/embeddings` test in LM_STUDIO_GUIDE.md section 4; fix the id; press *Save embedder*. If you want to keep working: set provider back to *Built-in (lexical, offline)*. |
| Ingestion job **paused: Embedding model unavailable** | Nothing is lost; the checkpoint is kept. Fix LM Studio, then **Resume**. |
| Searches say "keyword search only. Re-index to fix." | The collection was built with a different embedder. *Collections > Re-index*. Memory has its own button (*Memory > Re-index memories*). |
| Re-index fails midway | The embedder went away; start LM Studio and press Re-index again (it restarts that collection from scratch). |
| Dimension/"embedder changed" confusion after switching vector store | Press Re-index on every collection (vectors are not migrated between ChromaDB and the SQLite store). |
| Chat still answers but without sources | Retrieval degraded (embedder down): the answer bubble says so. Fix the embedder; keyword search keeps working meanwhile. |

## Retrieval / knowledge

| Symptom | Fix |
|---|---|
| Search finds nothing | Check *Knowledge > Collections* shows chunks > 0. Unrelated text is intentionally not returned. Try different words (the built-in embedder is lexical). |
| Collection shows **needs re-index** | The embedding model changed. Press *Re-index*. Until then only keyword search works. |
| Answers ignore my documents | Make sure *Use my knowledge* is ticked and the right collection is selected; look at the sources panel under the answer. |
| PDF upload: everything rejected as "low-information" / job paused by the quality gate / "N page(s) are scanned images" | The PDF is a **scan** (pages are pictures with no text layer); only stray page numbers/footers can be read, and **OCR is not built into NEXUS**. Check: open the PDF in a viewer and try to select/copy a sentence - if you cannot, it is a scan. Fix: run OCR first, then upload the OCR'd copy. Options: Adobe Acrobat *Scan & OCR > Recognize Text*; or free `ocrmypdf` (`pip install ocrmypdf`, plus the Tesseract OCR and Ghostscript installers for Windows) then `ocrmypdf scan.pdf scan_ocr.pdf`; or export the text yourself to a `.txt`/`.docx` and upload that. Diagnostic from the `backend` folder: `..\.venv\Scripts\python.exe -c "import pymupdf; d=pymupdf.open(r'C:\path\file.pdf'); print(len(d), 'pages'); [print(i+1, 'text chars:', len(p.get_text()), 'images:', len(p.get_images())) for i, p in enumerate(d) if i < 8]"` - near-zero text chars with images >= 1 on every page confirms a scan. |
| PDF shows "page has no extractable text" | The PDF is a scan. OCR is not built in; run OCR first (e.g. in Acrobat/ocrmypdf). |
| Ingestion job **paused: Quality gate** | Too many rejected/quarantined chunks. Open *Rejected records*, fix the filter or the source, then Resume (or "Resume anyway"). |
| Job **paused: Embedding model unavailable** | LM Studio stopped or the embedding model is not loaded. Fix it and Resume - the checkpoint is kept. |
| Job **interrupted** after a crash/restart/closed window | Open the job and press **Resume**; it continues from the last committed batch (no duplicates are created; this was tested with a simulated hard crash). Items that show *failed* (e.g. a file was locked) are retried with the **Retry N failed** button. If you cannot find the job, *Data ingestion > Jobs*. |
| `nexus.cli ingest` fails with a vector-store/lock error while the app is running | Stop NEXUS first (`scripts\stop.ps1`); use the web UI while it runs. |
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
