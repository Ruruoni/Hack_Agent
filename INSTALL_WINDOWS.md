# NEXUS AI - Windows installation guide (for beginners)

This guide assumes you have **never** used Python environments, Node.js, APIs or embeddings. Every command is for **PowerShell**.
It takes about 30-60 minutes, most of it downloads. You do not need to understand the terms below to follow it - they are explained as they appear.

> **Verification status:** the application and its tests were built and run on Linux. The Windows scripts were parse-checked and their start/stop/port logic was exercised under PowerShell Core on Linux, but **nothing has been executed on Windows yet - you are the first Windows run.** Anything that fails: copy the message and see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md). The quick version of this guide is [docs/FIRST_RUN_CHECKLIST.md](docs/FIRST_RUN_CHECKLIST.md).

> **What you are installing:** a program that runs on your own computer. It talks to **LM Studio** (a separate app that runs the AI model locally).
> Nothing is sent to the internet unless you ask it to fetch a web page or download a dataset.

---

## 0. What you need (check the boxes as you go)

| # | Software | Why it is required | Official download |
|---|---|---|---|
| 1 | **Python 3.11 or 3.12** | The backend (the "brain" server) is written in Python. | https://www.python.org/downloads/windows/ |
| 2 | **Node.js (LTS)** | Builds the web interface you see in the browser. | https://nodejs.org/ |
| 3 | **LM Studio** | Runs the language model on your PC. | https://lmstudio.ai/ |
| 4 | **VS Code** (recommended) | A friendly editor with a built-in terminal. | https://code.visualstudio.com/ |
| 5 | **Git** (optional) | Only if you want to `git clone` instead of downloading a ZIP. | https://git-scm.com/download/win |

Hardware: 16 GB RAM is comfortable; 8 GB works with small models. A GPU is **not** required for NEXUS itself; it only makes the model faster. Keep at least 20 GB of free disk space (models are big).

> Python 3.13 also works for the core app, but some optional packages (ChromaDB, PyTorch) publish Windows wheels later for new Python versions. **3.12 is the safest choice.**

---

## 1. Install Python

1. Download the **Windows installer (64-bit)** from the link above and run it.
2. **Tick the box "Add python.exe to PATH"** on the first screen (this is the #1 cause of problems if you forget), then click *Install Now*.
3. Close any open PowerShell windows, open a **new** one (press `Win`, type `PowerShell`, Enter) and verify:

```powershell
python --version
```

You should see `Python 3.12.x` (or 3.11.x). If you see "not recognized", Python is not on PATH: re-run the installer, choose *Modify*, and tick *Add Python to environment variables*.

## 2. Install Node.js

Run the LTS installer with default options, open a **new** PowerShell and verify:

```powershell
node --version
npm --version
```

Both should print version numbers (Node 18 or newer).

## 3. Install VS Code (recommended)

Install it with default options. Then install the **Python** and **PowerShell** extensions (VS Code will suggest them when you open the project).

## 4. Get the project

The repository is public: **https://github.com/Ruruoni/Hack_Agent**. The code lives on the branch **`claude/epic-faraday-mbnov9`**, which is also the repository's default branch (there is no `main`).

**Option A - Git (recommended).** In PowerShell (any folder you can write to, without spaces if possible):

```powershell
mkdir C:\Projects -ErrorAction SilentlyContinue
cd C:\Projects
git clone https://github.com/Ruruoni/Hack_Agent.git
cd Hack_Agent
git checkout claude/epic-faraday-mbnov9
git log -1 --format="%H  %s"
git status --short
```

* `git log` prints the commit you are on; compare the hash with the one in the handover message. `git status --short` must print nothing (clean checkout).
* To update later: `git pull origin claude/epic-faraday-mbnov9`, then re-run `scripts\setup.ps1`.
* Behind a corporate proxy: `git config --global http.proxy http://host:port`.

**Option B - ZIP.** Open https://github.com/Ruruoni/Hack_Agent/archive/refs/heads/claude/epic-faraday-mbnov9.zip in a browser, right-click the downloaded file > *Extract All* to `C:\Projects`, and rename the folder to `Hack_Agent`. (A ZIP has no Git history, so you cannot compare commit hashes.)

Open the folder in VS Code: *File > Open Folder...* > `Hack_Agent`. Open the terminal with **Ctrl + `** (PowerShell, already in the project folder). All commands below are run from the project folder unless stated otherwise.

## 5. Run the one-time setup script

PowerShell blocks scripts by default, so we start the script with an explicit permission flag that applies **only to this one command**:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

What it does (six numbered steps): finds Python (3.10+, preferring 3.12), creates a private environment in `.venv\`, installs the Python packages, copies `.env.example` to `.env`, installs and builds the web interface (needs Node.js), and creates the database in `data\`. It is safe to re-run; existing `.venv`, `.env` and `data\` are kept.

* It takes 3-10 minutes. Red text from `pip` about *"dependency resolver"* warnings is usually harmless; a final green **"Setup finished."** line means success. A yellow **"Setup finished WITHOUT the web UI"** means Node.js/npm needs fixing (the API works, the browser UI does not).
* If the optional ChromaDB package fails to install you will see a yellow message. That is fine: NEXUS automatically uses its built-in SQLite vector store.
* To also install the **optional** fine-tuning libraries (several GB): `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -WithTraining`. You can skip this - retrieval and memory work without it.

If anything fails, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## 6. Install LM Studio and load a model

Follow [LM_STUDIO_GUIDE.md](LM_STUDIO_GUIDE.md) (10 minutes + model download). In short: install LM Studio, download one chat model and one embedding model, load the chat model, open the **Developer** tab and **Start Server**.

## 7. Start NEXUS

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

The script checks that port 8000 is free, starts the backend hidden in the background, waits for it to answer, then opens **http://127.0.0.1:8000**. If the port is taken it tells you and suggests `-Port 8010`; if the backend fails it prints the last lines of `data\logs\backend.err.log`. The top bar shows *LM Studio connected* and the active model name when everything is wired up.

Open it with `127.0.0.1` or `localhost` - other host names are refused on purpose (see docs/SECURITY.md).

Stop it any time with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
```

VS Code shortcut: **Terminal > Run Task...** lists *NEXUS: start*, *NEXUS: stop*, *NEXUS: run all tests*, *NEXUS: check installation*.

## 8. First-run wizard

Open **Setup wizard** in the left menu and press **Run all basic checks**. That button runs 13 of the 15 checks in order: Python, Node.js, project folders, LM Studio connection, model discovery, model selection, embedding model, database, knowledge base, a test reply from the model, a sample document upload, semantic retrieval, and a small dataset import. Two checks are run by hand because they need your input:

* **Public web page import** - keep `https://example.com` or enter another public URL, then press *Run*.
* **Memory persists after restart** - press **Write marker**, run `scripts\stop.ps1` then `scripts\start.ps1`, return to the wizard and press **Verify after restart**.

The wizard saves your model choice (`chat_model`) into `data\settings.json`; change it later under *Models & learning*. Prefer the command line? `powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1` runs the first ten checks in the terminal.

Then run the full end-to-end test: **[docs/ACCEPTANCE_TEST.md](docs/ACCEPTANCE_TEST.md)** (sample PDF + CSV + public web page + restart).

## 9. Your first useful session

1. **Chat > Upload** a PDF or text file. Ask a question about it. The answer shows numbered **sources** you can expand.
2. Say "Remember that I prefer short answers." - it is saved as a *verified preference*.
3. Switch to **Agent** mode and ask: *"List the files in my workspace and summarise them."* Anything consequential waits for your approval in the **Approvals** panel.
4. Try **Data ingestion > New job** with a folder of documents, or **Dataset manager** for CSV/JSON/Hugging Face datasets ([DATASET_GUIDE.md](DATASET_GUIDE.md)).

## 10. Where things live

| Path | What |
|---|---|
| `data\nexus.db` | The SQLite database (conversations, memory, knowledge metadata, audit log). |
| `data\vectors\` | The vector index (rebuildable with *Re-index*). |
| `data\raw\`, `data\uploads\`, `data\downloads\`, `data\web\` | Preserved original inputs. |
| `data\workspace\` | The agent's scratch folder (the only place it may write without extra permission). |
| `data\reports\`, `data\exports\` | Reports and exports. |
| `data\trash\` | Files the agent "deleted" (recoverable). |
| `data\backups\` | Backups. |
| `data\logs\` | Backend logs (`backend.log`, `backend.err.log`). |
| `.env` | Your configuration (also editable in the web UI under *Settings*). |

To move the data to another drive, set `NEXUS_DATA_DIR=D:\NexusData` in `.env` and restart.

## 11. Updating, backing up, uninstalling

* **Backup:** *Settings > Backup, audit & history > Create backup now* (or `scripts\backup.ps1`). **Restore:** stop NEXUS, then from the project folder: `cd backend` and `..\.venv\Scripts\python.exe -m nexus.cli restore ..\data\backups\<file>.zip`; start NEXUS and press *Re-index* on every collection (vectors are not part of the backup).
* **Update the code:** replace the project files (keep `data\` and `.env`), re-run `scripts\setup.ps1`.
* **Uninstall:** delete the project folder. LM Studio and its models are separate.

## Choosing the vector store (read once)

If the optional ChromaDB package installed, NEXUS uses it (`NEXUS_VECTOR_BACKEND=auto`); otherwise it uses its built-in SQLite vector store. Both work. ChromaDB on Windows is **untested** here: if NEXUS fails to start with a `chromadb`/`hnswlib` error, set `NEXUS_VECTOR_BACKEND=sqlite` in `.env` and restart. **Decide before ingesting data:** vectors are not moved between stores, so if you change the backend later, press *Re-index* on every collection. Only one NEXUS process may use the vector store at a time (do not run `nexus.cli ingest` while the app is running).

## Security notes for beginners

* NEXUS listens on `127.0.0.1` only (your own computer). Do not expose port 8000 to a network.
* The default mode is **approval required**: the agent asks before it writes outside its workspace, deletes, runs code or touches the network in consequential ways.
* Never paste passwords or tokens into chat. Dataset tokens (`HF_TOKEN`) go in `.env` or a Windows environment variable.
