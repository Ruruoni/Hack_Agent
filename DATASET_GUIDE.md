# Dataset guide

How to get data into NEXUS: local files, public URLs, and Hugging Face datasets - with the GUI or the command line.

> **Ingesting a dataset makes it searchable. It does not train or change the language model.** Fine-tuning is a separate, optional, approval-gated feature (see README).

## 1. Supported inputs

PDF (text-based, **scanned, or mixed**), **images (PNG, JPG, TIFF incl. multi-page, BMP, GIF, WEBP)**, DOCX (including pictures inside it), TXT, Markdown, HTML, CSV/TSV, JSON, JSONL/NDJSON, XLSX, XML, **Parquet**, source code, config files (ini/toml/yaml/env...), log files, and ZIP/TAR/GZ **archives** of those (members are extracted into a safe workspace with zip-slip and zip-bomb protection). Public URLs are fetched and text-extracted. Other formats: add a parser in `backend/nexus/ingestion/parsers.py` (`PARSERS` registry).

Large files are **streamed**: PDFs page by page, CSV/JSONL/Parquet in batches, JSON arrays incrementally, XLSX in read-only mode. A top-level JSON *object* over 50 MB cannot be streamed - convert it to JSONL.

## 2. GUI workflow

### Local files and folders (Data ingestion > New job)
1. Paste paths (one per line) or press **Browse...**.
2. **Estimate size and time** (also suggests a batch size from your free RAM and checks disk space).
3. Choose a **collection** (a named knowledge set; it is created if missing).
4. Optional **filtering and quality rules**: regexes to keep or reject, the reject-rate that pauses the job, table rows indexed or profile-only.
5. **Start ingestion**. Watch progress, then open the tabs:
   * **Files** - every input with status (done, duplicate, rejected, quarantined, failed) and the reason.
   * **Rejected records** - malformed CSV rows, bad JSON lines, encoding problems, extraction errors, with samples.
   * **Quarantine** - chunks that look like instructions aimed at an AI. Not searchable until *you* release them.
   * **Schema & quality** - per-column types, missing-value rate, and a warning if the record structure changed inside the file.
   * **Report** - the full batch report, including a retrieval self-check. Copy or find it later in `data\reports\`.
6. **Pause / Resume / Cancel / Retry N failed** (the button shows the count) at any time. If the app or PC stops, the job becomes *interrupted*; press **Resume** and it continues from the last checkpoint.

### Dataset manager (Data ingestion > Dataset manager)
1. Enter a **Hugging Face id** (`owner/name`), a **direct URL**, or a **local path**, then **Inspect**. You see licence, whether login is required, every file with its size and split (train/test/validation), and warnings.
2. Tick the files/splits you want, press **Check size, disk space and access**.
3. **Download.** Anything bigger than *Settings > Dataset downloads above this need explicit approval* (default 500 MB) additionally requires you to tick an approval box. Downloads resume after interruptions and are verified (size, and SHA-256 when the source provides it).
4. **Preview** shows columns and the first records. **Import into pipeline** feeds the files into the same ingestion engine as above. Originals stay untouched in `data\downloads\`.

**Private or gated Hugging Face datasets:** accept the dataset's terms on huggingface.co, create a *read* token (https://huggingface.co/settings/tokens) and put it in `.env` as `HF_TOKEN=...` (or a Windows environment variable), then restart NEXUS. **Never paste tokens into the chat.** NEXUS only reads the token from the environment.

## 3. Command line

> **Stop NEXUS first** (`scripts\stop.ps1`) before using `ingest` or `dataset import` on the command line: if ChromaDB is installed its vector store must be used by one process at a time. `dataset inspect` and `dataset plan` only read and plan, but every CLI command opens the application data folder, so the safest habit is to stop NEXUS before any CLI command.

Run from the `backend` folder with the project's Python (`..\.venv\Scripts\python.exe` on Windows). The Hugging Face ids below are **examples only - not verified from the build environment** (Hugging Face was unreachable there); dataset ids move, so confirm them on https://huggingface.co/datasets:

```powershell
cd backend
..\.venv\Scripts\python.exe -m nexus.cli ingest C:\data\manuals C:\data\notes.md --collection manuals
..\.venv\Scripts\python.exe -m nexus.cli dataset inspect cornell-movie-review-data/rotten_tomatoes
..\.venv\Scripts\python.exe -m nexus.cli dataset plan cornell-movie-review-data/rotten_tomatoes --splits train
..\.venv\Scripts\python.exe -m nexus.cli dataset download cornell-movie-review-data/rotten_tomatoes --splits train
..\.venv\Scripts\python.exe -m nexus.cli dataset download https://example.org/data.csv
..\.venv\Scripts\python.exe -m nexus.cli dataset import <download id printed above> --collection imdb
```

Add `--approve-large` only after you have read the size/disk-space plan.

## 4. What happens to every file (the pipeline)

discover -> record source and metadata -> detect type -> validate integrity -> extract -> detect malformed records/encodings -> normalize (without removing words, numbers or structure) -> exact and near-duplicate detection (SHA-256 + simhash) -> quality and relevance filters -> **quarantine** questionable chunks (nothing is silently discarded; every rejection is logged with a reason) -> chunk -> embed -> index -> extract simple facts (low confidence, never auto-trusted) -> link to source -> retrieval self-check -> report.

* **Checkpoints:** every batch of chunks is committed in one database transaction together with the file's resume position, so a crash can never leave the progress marker ahead of the data.
* **Bounded resources:** a fixed number of workers, `Chunks per checkpoint` batches, a free-disk-space check, per-item time limit, file-size limit.
* **Quality gate:** if more than the configured share of judged chunks is rejected or quarantined (default 60% after 50 chunks), the job *pauses* instead of publishing junk. Inspect the rejects, adjust the filters (job config) and Resume - or "Resume anyway".
* **Duplicates:** identical files are skipped; identical and near-identical chunks are not indexed twice. Repetition is never treated as evidence of truth.
* **Retry:** failed items (e.g. a locked file) can be retried; rejected items (corrupt, unsupported) stay rejected unless you ask to retry them too.

## 5. Preparing data for optional fine-tuning

Training data is a **separate** pipeline (*Models & learning > Fine-tuning*): JSONL with `messages`, or `prompt`+`completion`, or `instruction`+`output`. You must state the licence and confirm you may use it; duplicates, low-quality rows and instruction-injection text are removed; the train/validation/test split is deterministic and leak-checked. A demo file is in `samples\training_demo.jsonl`.

## 6. Tips

* One topic per collection (e.g. `networking-docs`, `ticket-history`) makes retrieval cleaner and deletion easy.
* Scanned PDFs and images are read with the built-in OCR (about 1-3 s per page, CPU). Clean, straight, 200+ dpi printed text works best; handwriting and complex layouts do not. Low-confidence OCR pages are flagged in the report. Details: docs/TROUBLESHOOTING.md > PDFs, images and OCR.
* After changing the embedding model, press **Re-index** on each collection.
* To delete a dataset's knowledge: *Knowledge > Collections > Delete*. The raw originals in `data\raw\` / `data\downloads\` are kept until you delete them yourself.

## 7. Your first Hugging Face import: small first (step by step)

**Never start with a large dataset.** Prove the whole chain on something tiny, then scale.

1. **Pick a small, text-only dataset.** On the dataset's Hugging Face page check: the **licence** (card sidebar), **"Size of downloaded dataset files"** (aim for under ~5 MB for the first test), and that it is *not* gated (no "You need to agree to share your contact information" banner). Prefer text columns; image/audio datasets are not useful here. Example family: a small text-classification dataset such as `cornell-movie-review-data/rotten_tomatoes` (unverified id, see above).
2. **Inspect without downloading** (GUI: *Data ingestion > Dataset manager > Inspect*; or CLI from `backend` with NEXUS running or stopped):

```powershell
..\.venv\Scripts\python.exe -m nexus.cli dataset inspect cornell-movie-review-data/rotten_tomatoes
```

   Read the output: `license` (a value, or "not declared" - then find out the terms yourself before using the data), `requires_auth` / `gated`, every file with its **size in bytes** and detected split (`train`/`test`/`validation`), and `total_bytes`. Check the licence permits your use.
3. **Plan the smallest download** - one file or one split - and read the size/disk verdict:

```powershell
..\.venv\Scripts\python.exe -m nexus.cli dataset plan cornell-movie-review-data/rotten_tomatoes --splits train
```

   `needs_explicit_approval: true` means it exceeds the automatic limit (default 500 MB, adjustable in Settings): stop and reconsider for a first test. `disk_ok` must be true.
4. **Download** (NEXUS stopped if you use the CLI; or press *Download* in the GUI):

```powershell
..\.venv\Scripts\python.exe -m nexus.cli dataset download cornell-movie-review-data/rotten_tomatoes --splits train
```

   The output prints a download `id`, `status: ready` and the destination folder under `data\downloads\`. Gated datasets fail with a message that tells you to set `HF_TOKEN` in `.env` (never paste tokens into chat).
5. **Confirm the originals are preserved.** The files sit unchanged in `data\downloads\<dataset>_<id>\`. NEXUS only reads them; ingestion indexes copies of the *text*, never edits the files, and does not copy them again. To prove it:

```powershell
Get-FileHash data\downloads\*\* -Algorithm SHA256   # note the hashes, ingest, run it again: identical
```

   When the source provides checksums (large LFS files) NEXUS verifies them automatically and marks the download `failed` with "SHA-256 mismatch" if they differ.
6. **Preview, then import into a test collection.** GUI: *Preview* then *Import into pipeline* with collection `hf-test`. CLI: `..\.venv\Scripts\python.exe -m nexus.cli dataset import <download id> --collection hf-test`.
7. **Check the batch report** (printed by the CLI; *Report* tab in the GUI): records read vs the file's known row count, **rejected** (and why), **duplicates**, **quarantined**, retrieval self-check recall. Then run two searches in *Knowledge & memory > Search* using words you can see in the preview. Expected: results from the dataset file with locators like `rows 1-25`.
8. **Only then scale up.** Use *Estimate size and time* on the bigger source, keep batch size at the suggested value, watch free disk space (originals + index), and expect the quality gate to *pause* the job rather than index junk. Interrupt it once on purpose (press *Pause* or close NEXUS) and confirm *Resume* continues - the same check you rely on for big imports.

Datasets with huge rows or non-text content (images, audio, nested binary columns) produce poor chunks; skip them.
