# Acceptance test: PDF + CSV + public web page + restart

A small, repeatable test that proves, on **your** computer, that ingestion, retrieval, source citation and persistence work.
Allow 20-30 minutes. Fill in the result sheet at the end.

**Where the expected numbers come from:** the PDF and CSV numbers below were *measured* by running the same sample files through this code (Linux build environment, built-in lexical embedder). The web-page expectation could **not** be measured there (no internet) - it is reasoned from the code and marked as such. Anything marked **YOUR RESULT** must be filled in by you.

## 0. Prerequisites

* `scripts\setup.ps1` finished and `scripts\start.ps1` shows NEXUS running at http://127.0.0.1:8000 ([INSTALL_WINDOWS.md](../INSTALL_WINDOWS.md)).
* Top bar: **LM Studio connected** + your model name ([LM_STUDIO_GUIDE.md](../LM_STUDIO_GUIDE.md)). Parts A-C and F work even without LM Studio; Part D (chat answers) needs it.
* Sample files already in the project: `samples\sample_manual.pdf` (2 pages), `samples\tickets.csv` (200 rows + 1 deliberately broken row). Get their full paths:

```powershell
(Resolve-Path samples\sample_manual.pdf).Path
(Resolve-Path samples\tickets.csv).Path
```

* Clean slate (optional, for re-runs): **Knowledge & memory > Collections > Delete** the collection named `acceptance`.

## A. Ingest the PDF and the CSV (GUI)

1. **Data ingestion > New job**. In *Files, folders or public URLs* paste the two full paths, one per line.
2. *Collection:* `acceptance`. *Job name:* `acceptance-test`. Press **Start ingestion**. Open the job under **Jobs**.

**Expected (measured):**

| Item | Expected |
|---|---|
| Job status | `completed` |
| Items discovered / processed | 2 / 2 |
| Chunks indexed | **26** (PDF 2 + CSV 24) |
| Rejected | **1** (the broken CSV row) |
| Duplicates / quarantined / failed | 0 / 0 / 0 |
| *Rejected records* tab | one row: `malformed row: 2 fields, expected 4`, sample `5999,broken row` |
| *Schema & quality* tab (tickets.csv) | 200 rows, 1 malformed, columns `ticket_id, category, priority, summary` |
| *Report* tab | counts above + "Retrieval self-check ... recall@5 = 1.0" |

(CLI equivalent, with NEXUS **stopped** - see the warning in section G:
`cd backend` then `..\.venv\Scripts\python.exe -m nexus.cli ingest ..\samples\sample_manual.pdf ..\samples\tickets.csv --collection acceptance --name acceptance-test`.)

## B. Ingest a public web page (needs internet)

1. **Data ingestion > New job**, source `https://example.com`, collection `acceptance`, **Start**.

**Expected (reasoned, NOT verified live):** status `completed`; 1 item; at least 1 chunk; the *Files* tab shows the URL as the source (not a local path). The page text is the short "Example Domain ... This domain is for use in illustrative examples in documents" paragraph.

If it fails, the *Files* tab shows why: `fetch failed: ...` (network/proxy/firewall), `blocked` reasons are listed in **Web research** (robots.txt, login, CAPTCHA are never bypassed), or "resolves to a private address" (SSRF guard - not expected for example.com). A richer page also works, as long as it allows automated access.

## C. Check the knowledge base

**Knowledge & memory > Collections:** `acceptance` shows 3 documents (`sample_manual.pdf`, `tickets.csv`, `example.com`) and about 27 chunks (26 + the web page's). Click a document to **inspect its source**: raw file path, SHA-256, each chunk with its locator (`page 1`, `rows 1-25` ...).

**Knowledge & memory > Search**, one query at a time. Expected top result (measured with the lexical embedder):

| Query | Expected first hit |
|---|---|
| `Which extension does the Orion support desk use?` | `sample_manual.pdf`, `page 2`, text contains **4417** |
| `How long are Orion snapshots kept and how are they encrypted?` | `sample_manual.pdf`, `page 1`, contains **45 days** and **AES-256** |
| `case 187` | `tickets.csv`, `rows 176-200` |
| `What is the example domain for?` | the `example.com` page (not verified live) |

## D. Grounded chat (needs LM Studio)

**Chat**, choose *Collections:* `acceptance`, leave *Use my knowledge* ticked, ask:

1. `Which extension does the Orion support desk use?`
2. `How long are Orion snapshots kept, and how are they encrypted?`
3. A question the documents cannot answer: `What is the CEO's salary?`

**Pass criteria:** answers 1-2 state 4417 / 45 days + AES-256, show a `[S1]`-style citation, and the *sources retrieved* panel names `sample_manual.pdf` with `page 2` / `page 1`. Answer 3 should say the sources do not contain it (honesty rule). Exact wording depends on your model; a small model may omit the citation tag - then judge by the sources panel and note it. **YOUR RESULT:** ______

## E. Memory

In Chat say: `Remember that the acceptance test passphrase is blue-falcon.` Then **Knowledge & memory > Memory > semantic**: one entry, trust **user verified**.

## F. Persistence after restart

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Then verify, without ingesting anything again:

1. Collections still show `acceptance` with the same document/chunk counts.
2. Chat history list (left side) still contains your earlier conversation with its messages.
3. Repeat the Part C search `Which extension does the Orion support desk use?` - same hit.
4. In a **new** chat ask `What is the acceptance test passphrase?` - answer `blue-falcon` (retrieved from memory; the sources panel shows a `memory` source).
5. **Setup wizard > Memory persists after restart**: press *Write marker*, restart again, press *Verify after restart* - must pass.

## G. Inspecting failures

| Where | What it tells you |
|---|---|
| Job > **Files** tab | per-file status and reason (rejected/failed/duplicate), error text |
| Job > **Rejected records** | each malformed record with reason and sample |
| Job > **Quarantine** | chunks held back because they look like instructions to an AI |
| Job banner | `paused` reason (quality gate, embedding model unavailable) |
| **Settings > Backup, audit & history** | audit log of settings changes, approvals, blocked requests |
| `data\logs\backend.err.log` | Python errors (`Get-Content data\logs\backend.err.log -Tail 40`) |
| `scripts\doctor.ps1` | connection / model / database checks with hints |
| [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) | symptom -> fix table |

**Warning - one process at a time:** if ChromaDB is installed, the vector store lives in `data\vectors` and must be opened by **one** NEXUS process. Use the web UI while NEXUS runs; use `nexus.cli ingest/dataset import` only when NEXUS is stopped.

## H. Optional: repeat with real semantic embeddings

After the test passes with the lexical embedder, follow [LM_STUDIO_GUIDE.md](../LM_STUDIO_GUIDE.md) sections 4-5 to switch to an LM Studio embedding model and **Re-index**, then repeat Part C and Part D. Compare `how long do we keep backups`: with the lexical embedder the measured first hit was a `tickets.csv` chunk (wrong) and the PDF page 1 second; with a good semantic embedder the PDF page should rank first. **YOUR RESULT:** ______ (this improvement has not been measured by the authors).

## Result sheet

| Step | Expected | YOUR RESULT (pass/fail + note) |
|---|---|---|
| A. PDF+CSV job | completed, 26 chunks, 1 rejected | |
| B. Web page | completed, URL preserved | |
| C. Searches | 4 expected first hits | |
| D. Grounded chat | correct answers + sources; 3rd says "not in sources" | |
| E. Memory | verified entry created | |
| F. Restart persistence | all 5 checks | |
