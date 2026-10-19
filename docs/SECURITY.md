# Security and permission model

**Principle:** the language model can only *propose* actions. Whether anything runs is decided by code and by settings that the model cannot read or change.

## Execution modes (Settings > Permissions)

| Mode | Behaviour |
|---|---|
| `read_only` | Only read effects run (optionally web reads). Writes, memory writes, code, deletes and network writes are denied. |
| `approval_required` (default) | Reads, web reads, memory notes and writes inside the agent workspace run. Everything else (deletes, writes elsewhere, code execution, ingestion, network writes, installs, training) waits for a human. |
| `restricted_automation` | Additionally runs tools on your explicit allow-list automatically if they are low/medium risk and not destructive. Sandboxed Python is the only high-risk tool an allow-list entry can unlock. Deletes, installs, training and network writes are never automatic. If untrusted content with embedded instructions was seen in the task, even allow-listed non-read actions need approval again. |

Network diagnostics additionally require every target to be in **Authorized targets**; outside that list the call is **denied** (not approvable).

## What the agent cannot do

* There is no tool that reads or writes settings, permissions, approvals, the database file, `.env`, logs, vectors or the training area. File tools resolve symlinks first and only accept the workspace, report/export/upload/raw/download/web folders and folders you added.
* Approvals are decided only through `POST /api/approvals/{id}/decide`; the engine refuses decisions attributed to `agent`/`model`/`system`, binds each approval to a hash of the exact arguments and burns it after one use. The same rule protects skill approval, extension activation/dependency install, training start, model accept/activate.
* No unrestricted shell. `run_command` takes a registered command id plus validated parameters (regex per parameter, no leading `-`, no newlines) and runs without a shell. External utilities (e.g. nmap) exist only if **you** register them in Settings.
* Python execution: AST allow-list (no imports outside a small stdlib list, no `open/eval/exec/__import__/getattr`, no dunder access) + restricted builtins + child process with scrubbed environment, temp working directory, POSIX rlimits (CPU, memory, file size, no processes) and a parent watchdog for wall-time/memory/output (works on Windows). **This is defence in depth, not a hardened sandbox**: run hostile code in a VM/container. Every run needs approval unless you allow-list it.
* Deleted files are moved to `data\trash` (restorable); overwritten files are backed up to `data\backups`.

## Untrusted content

Documents, web pages, tool outputs and memories are data. They are fenced (`<untrusted_source>`, `<untrusted_tool_output>`), scanned for instruction-like text (ignore-previous, role overrides, safeguard bypass, self-approval, exfiltration, hidden-from-user, pipe-to-shell...), flagged in the UI and audit log, **quarantined** at ingestion (not searchable until a human releases them), and mark the task as *tainted*. Training data containing such text is excluded so it cannot be baked into weights. Pattern scanning cannot catch every attack - the permission engine is the real boundary.

## Web safety

http/https only; allow/deny lists; every redirect hop is re-checked; DNS results that are loopback/private/link-local/reserved are blocked (SSRF) unless a human enables *Allow private hosts*; the cloud metadata address is always blocked; robots.txt respected; per-host rate limit; size limit; login/paywall/CAPTCHA/anti-bot responses are recorded as `blocked` and **not bypassed**. DNS rebinding between the check and the connection is not fully prevented.

## API surface

Binds to `127.0.0.1`. Mutating requests carrying a browser `Origin` outside the allow-list are rejected (a malicious web page cannot drive your local agent). There is no user authentication: anyone with local access to the machine/port can use it. Do not expose it to a network without a reverse proxy providing authentication. `/api/fs/browse` lists local directories for the folder picker for the same reason.

## Secrets and privacy

API keys/tokens are read from environment variables or `.env`/Settings (masked in the API). Dataset tokens are never accepted through chat. No telemetry is sent (the setting exists to make that explicit). Chroma telemetry is disabled.

## Audit

`audit_log` is append-only by convention and hash-chained (`prev_hash`, `hash`); *Settings > Backup, audit & history* shows the chain status. It records settings changes, approvals, tool attempts/denials, injection detections, deletions, training decisions, backups.

## Known limitations

* The hash chain detects tampering, but a local administrator could rebuild the whole chain.
* Prompt-injection detection is heuristic. Rely on approval mode for anything that matters.
* Extension plugins that a human activates run in-process with full Python privileges.
