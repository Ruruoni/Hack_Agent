# Dataset guide

How to get data into NEXUS: local files, public URLs, and Hugging Face datasets - with the GUI or the command line.

> **Ingesting a dataset makes it searchable. It does not train or change the language model.** Fine-tuning is a separate, optional, approval-gated feature (see README).

## 1. Supported inputs

PDF, DOCX, TXT, Markdown, HTML, CSV/TSV, JSON, JSONL/NDJSON, XLSX, XML, **Parquet**, source code, config files (ini/toml/yaml/env...), log files, and ZIP/TAR/GZ **archives** of those (members are extracted into a safe workspace with zip-slip and zip-bomb protection). Public URLs are fetched and text-extracted. Other formats: add a parser in `backend/nexus/ingestion/parsers.py` (`PARSERS` registry).

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
6. **Pause / Resume / Cancel / Retry failed** at any time. If the app or PC stops, the job becomes *interrupted*; press **Resume** and it continues from the last checkpoint.

### Dataset manager (Data ingestion > Dataset manager)
1. Enter a **Hugging Face id** (`owner/name`), a **direct URL**, or a **local path**, then **Inspect**. You see licence, whether login is required, every file with its size and split (train/test/validation), and warnings.
2. Tick the files/splits you want, press **Check size, disk space and access**.
3. **Download.** Anything bigger than *Settings > Dataset downloads above this need explicit approval* (default 500 MB) additionally requires you to tick an approval box. Downloads resume after interruptions and are verified (size, and SHA-256 when the source provides it).
4. **Preview** shows columns and the first records. **Import into pipeline** feeds the files into the same ingestion engine as above. Originals stay untouched in `data\downloads\`.

**Private or gated Hugging Face datasets:** accept the dataset's terms on huggingface.co, create a *read* token (https://huggingface.co/settings/tokens) and put it in `.env` as `HF_TOKEN=...` (or a Windows environment variable), then restart NEXUS. **Never paste tokens into the chat.** NEXUS only reads the token from the environment.

## 3. Command line

Run from the `backend` folder with the project's Python (`..\.venv\Scripts\python.exe` on Windows):

```powershell
cd backend
..\.venv\Scripts\python.exe -m nexus.cli ingest C:\data\manuals C:\data\notes.md --collection manuals
..\.venv\Scripts\python.exe -m nexus.cli dataset inspect stanfordnlp/imdb
..\.venv\Scripts\python.exe -m nexus.cli dataset plan stanfordnlp/imdb --splits train
..\.venv\Scripts\python.exe -m nexus.cli dataset download stanfordnlp/imdb --splits train
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
* Prefer text-based PDFs. Scanned PDFs have no extractable text (reported per page); OCR is not built in.
* After changing the embedding model, press **Re-index** on each collection.
* To delete a dataset's knowledge: *Knowledge > Collections > Delete*. The raw originals in `data\raw\` / `data\downloads\` are kept until you delete them yourself.
