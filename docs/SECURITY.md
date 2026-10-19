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

## API surface and network exposure

* `scripts\start.ps1` and `nexus.cli serve` bind to `127.0.0.1` (this computer only). Do **not** pass `--host 0.0.0.0` and do not port-forward 8000: there is **no user authentication** - anyone who can reach the port can read your knowledge base, approve actions through the API, and browse your disks through `/api/fs/browse`.
* **Host header guard (DNS-rebinding defence):** every request must be addressed to `localhost`, `127.0.0.1` or `[::1]` (`allowed_hosts` setting); anything else gets `403 host_blocked`. This stops a hostile web page from rebinding its own domain to 127.0.0.1 and talking to the app as if it were same-origin.
* **Origin guard:** mutating requests carrying a browser `Origin` outside the allow-list get `403 origin_blocked`.
* Neither guard stops other *programs* on your computer (they can send any headers), nor another user logged in to the same machine. They protect against malicious **web pages** in your browser. A real login (reverse proxy with authentication) would be required for any shared or networked use - not implemented.
* Verify exposure on your machine: `netstat -ano | findstr :8000` must show `127.0.0.1:8000` as LISTENING, never `0.0.0.0:8000` or your LAN address. (If Windows Defender Firewall ever asks whether to allow `python.exe` on private/public networks, choose **Cancel/deny**: NEXUS needs no inbound network access. Whether Windows prompts for a loopback-only listener was not tested.)

## Before you enable active tool execution (practical checklist)

"Active" means anything beyond reading: Python execution, registered commands, file writes/deletes outside the workspace, ingestion triggered by the agent, API integrations that write. Work through this in order and keep the *default* until each item is done:

1. **Keep `approval required` mode** and read every approval prompt (tool, risk, exact arguments). Approve once; there is no "always allow" in this mode. Use `restricted automation` only after weeks of confidence, and only with a minimal allow-list of read-only tools.
2. **Never allow-list `python_sandbox`, `run_command` or `api_call` casually.** The sandbox is *best effort*: AST allow-list + restricted builtins + a separate process with scrubbed environment and temp working directory. On **Linux** it also applies OS limits (CPU, memory, file size, no new processes) - these were tested. On **Windows** those OS limits are **not available**: only the parent watchdog (wall-clock, memory polling every 50 ms, output cap) applies, there is no process-count limit, and the sandbox was **never executed on Windows**. Treat it as untrusted-code-weak there. For hostile code use a VM/container.
3. **Run NEXUS as a normal (non-administrator) Windows user**, ideally a dedicated account or a VM, so a mistake cannot touch other users' files or system settings.
4. **Keep the file scope tiny.** The agent may read/write only `data\workspace` and the folders you list under *Settings > Permissions > Extra folders*. Never add `C:\`, your user profile, `Documents` or a folder with secrets. Windows file permissions are not changed by NEXUS; the folder list *is* the boundary. Put test material in `data\workspace` instead.
5. **Authorized targets:** leave the list empty until you need network diagnostics, then add only hosts/CIDRs you own or are explicitly permitted to test (lab ranges). Do not register `nmap` or similar utilities unless you have written authorization; every such call still needs approval.
6. **Web:** keep *Allow private hosts* off (it exists for local lab pages only); keep robots.txt on; use the domain allow-list for research.
7. **Treat retrieved content as hostile.** Documents and web pages can contain instructions aimed at the model. NEXUS fences, flags and quarantines them and forces approvals after a flagged task, but detection is pattern based and can miss attacks. Do not ingest untrusted sources into the same collection as important ones, and read approval arguments even when the task "looks fine".
8. **Back up before experimenting:** *Settings > Backup, audit & history > Create backup now*. Deleted files go to `data\trash` (restorable); overwritten files are copied to `data\backups`.
9. **Watch the audit log** (*Settings > Backup, audit & history*) after the first sessions: look for unexpected tools, `injection.detected`, `api.blocked_host`/`blocked_origin` events.
10. **Extensions** (plugins) run with full Python privileges once you activate them. Activate only code you have read, and only after its self-test passes.
11. **Secrets:** keep API tokens in `.env`/environment variables, not in chat or documents; do not ingest folders containing `.env`, key files or password exports.
12. **Global STOP:** know where the red *STOP ALL* button is before you run long tasks; test it once on a harmless task.

## Secrets and privacy

API keys/tokens are read from environment variables or `.env`/Settings (masked in the API). Dataset tokens are never accepted through chat. No telemetry is sent (the setting exists to make that explicit). Chroma telemetry is disabled.

## Audit

`audit_log` is append-only by convention and hash-chained (`prev_hash`, `hash`); *Settings > Backup, audit & history* shows the chain status. It records settings changes, approvals, tool attempts/denials, injection detections, deletions, training decisions, backups.

## Known limitations

* The hash chain detects tampering, but a local administrator could rebuild the whole chain.
* Prompt-injection detection is heuristic. Rely on approval mode for anything that matters.
* Extension plugins that a human activates run in-process with full Python privileges.
