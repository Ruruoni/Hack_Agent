# First-run checklist (installation -> first successful end-to-end test)

Tick each box in order. Detailed explanations: [INSTALL_WINDOWS.md](../INSTALL_WINDOWS.md), [LM_STUDIO_GUIDE.md](../LM_STUDIO_GUIDE.md), [ACCEPTANCE_TEST.md](ACCEPTANCE_TEST.md). If a step fails, go to [TROUBLESHOOTING.md](TROUBLESHOOTING.md) before continuing.

## Part 1 - Install the tools (once)

- [ ] **Python 3.12 (64-bit)** from https://www.python.org/downloads/windows/ with *Add python.exe to PATH* ticked. Check: `python --version` shows 3.11 or 3.12 (3.10+ is the minimum).
- [ ] **Node.js LTS** from https://nodejs.org/. Check: `node --version` and `npm --version`.
- [ ] **Git** from https://git-scm.com/download/win. Check: `git --version`.
- [ ] **LM Studio** from https://lmstudio.ai/ and (recommended) **VS Code** from https://code.visualstudio.com/.
- [ ] Free disk space: at least 20 GB. RAM: 8 GB minimum, 16 GB comfortable.

## Part 2 - Get the code and set it up (once)

Open **PowerShell** (not as administrator):

- [ ] `cd C:\Projects` (any short folder you can write to; create it with `mkdir C:\Projects` if needed)
- [ ] `git clone https://github.com/Ruruoni/Hack_Agent.git`
- [ ] `cd Hack_Agent`
- [ ] `git checkout claude/epic-faraday-mbnov9` (this is also the repository's default branch)
- [ ] `git log -1 --format="%H %s"` - compare the hash with the one given in the handover message.
- [ ] `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` - ends with **Setup finished.** (a yellow "WITHOUT the web UI" means Node/npm needs fixing).
- [ ] Optional: open `.env` in Notepad and review it (defaults work). Nothing secret is required.

## Part 3 - LM Studio (once, then each session)

- [ ] LM Studio **Discover**: download one **chat model** (7-8B instruct, Q4, for 16 GB RAM; 3-4B for 8 GB) and one **embedding model** (e.g. nomic-embed-text).
- [ ] **Developer** tab: **Load** the chat model, then **Start Server** (default http://localhost:1234).
- [ ] PowerShell: `(Invoke-RestMethod http://localhost:1234/v1/models).data | % { $_.id }` lists your model ids. Write down the chat id and the embedding id.
- [ ] PowerShell: send a real chat request (snippet in LM_STUDIO_GUIDE.md section 3, step 2) - you get a sensible reply.

## Part 4 - Start NEXUS and run the wizard (each session)

- [ ] `powershell -ExecutionPolicy Bypass -File scripts\start.ps1` - prints **NEXUS is running** and opens http://127.0.0.1:8000.
- [ ] Top bar shows **LM Studio connected** and your model name.
- [ ] **Setup wizard** > *Run all basic checks* - all pass. (Steps 14 and 15 are run by hand: web import, and the memory marker.)
- [ ] **Models & learning > LM Studio & models** > *Test generation* returns a real answer from your model.

## Part 5 - Semantic search (recommended)

- [ ] **Models & learning** > *Embedding model*: provider *LM Studio embedding model*, model id = the embedding id you wrote down > **Save embedder**. Status shows *working (dim ...)* and no "lexical fallback" badge.
- [ ] If any collection is flagged *needs re-index*: **Knowledge & memory > Collections > Re-index** each one, and **Memory > Re-index memories**.

## Part 6 - The acceptance test

- [ ] Run **docs/ACCEPTANCE_TEST.md** parts A-F (PDF + CSV + web page, grounded chat, memory, restart).
- [ ] Expected: 26 chunks and 1 rejected record for the PDF+CSV job; correct, cited answers; everything still there after `stop.ps1` / `start.ps1`.

## Part 7 - Before you give the agent more power

- [ ] Leave **Settings > Permissions > Execution mode** on *approval required*. Do not add `python_sandbox` or `run_command` to the auto-allow list yet.
- [ ] `netstat -ano | findstr :8000` shows `127.0.0.1:8000` (not `0.0.0.0:8000`).
- [ ] Read "Before you enable active tool execution" in [SECURITY.md](SECURITY.md).
- [ ] Try one Agent task: `List the files in my workspace` - it runs; ask it to delete a file and confirm the **Approvals** panel appears and nothing happens until you press *Approve once*.
- [ ] Small dataset import test ([DATASET_GUIDE.md](../DATASET_GUIDE.md) section 7) before any large dataset.

## Done

You have a verified install when Part 4 and Part 6 are fully ticked. Record anything that failed (copy the message and `data\logs\backend.err.log`) - those are the items still unverified on your machine.
