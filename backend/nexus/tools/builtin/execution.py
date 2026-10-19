"""Execution-class tools: sandboxed Python, allow-listed commands, read-only SQL, reports, diagnostics, API calls."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil

from ...permissions.policy import EXEC, EXEC_SANDBOX, NET_READ, NET_WRITE, SCRATCH, PermissionDenied
from ..registry import Tool, ToolContext, ToolRegistry, fail, ok, truncate

RUNNER = Path(__file__).resolve().parents[1] / "sandbox_runner.py"


def _scrubbed_env() -> dict[str, str]:
    keep = {"PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL"}
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def run_limited(argv: list[str], *, stdin: str | None, cwd: str, timeout: float, mem_mb: int, out_limit: int,
                cancelled, env: dict | None = None) -> dict:
    """Run a child with wall-clock, memory (parent-polled, works on Windows), output and cancellation limits."""
    p = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, cwd=cwd, env=env or _scrubbed_env(), text=True, encoding="utf-8", errors="replace")
    t0 = time.time()
    killed = ""
    if stdin is not None:
        try:
            p.stdin.write(stdin)  # type: ignore[union-attr]
            p.stdin.close()       # type: ignore[union-attr]
        except OSError:
            pass
    ps = None
    try:
        ps = psutil.Process(p.pid)
    except psutil.Error:
        pass
    while p.poll() is None:
        time.sleep(0.05)
        if cancelled():
            killed = "cancelled"
        elif time.time() - t0 > timeout:
            killed = f"timed out after {timeout:.0f}s"
        elif ps is not None:
            try:
                if ps.memory_info().rss > mem_mb * 2**20 * 1.5:
                    killed = f"exceeded memory limit ({mem_mb} MB)"
            except psutil.Error:
                pass
        if killed:
            try:
                for c in (ps.children(recursive=True) if ps else []):
                    c.kill()
                p.kill()
            except psutil.Error:
                p.kill()
            break
    try:
        out, err = p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    return {"returncode": p.returncode, "stdout": out[:out_limit], "stderr": err[:out_limit // 2], "killed": killed,
            "seconds": round(time.time() - t0, 2)}


# ---------------------------------------------------------------- python sandbox
def python_sandbox(ctx: ToolContext, a: dict) -> dict:
    s = ctx.app.settings
    payload = json.dumps({"code": a["code"], "data": a.get("data"), "cpu_s": min(int(a.get("timeout", s.sandbox_cpu_s)), 60),
                          "mem_mb": s.sandbox_memory_mb})
    with tempfile.TemporaryDirectory(prefix="nexus_sbx_") as td:
        r = run_limited([sys.executable, "-I", "-S", str(RUNNER)], stdin=payload, cwd=td, timeout=min(int(a.get("timeout", s.sandbox_cpu_s)), 60),
                        mem_mb=s.sandbox_memory_mb, out_limit=60000, cancelled=ctx.cancelled)
    if r["killed"]:
        return fail(f"sandbox process {r['killed']}")
    try:
        res = json.loads(r["stdout"])
    except ValueError:
        return fail("sandbox produced no valid result", stderr=r["stderr"][:500], returncode=r["returncode"])
    return res


# ---------------------------------------------------------------- allow-listed commands
_HOST = r"[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?|[0-9a-fA-F:]{2,39}"
WIN = platform.system() == "Windows"
COMMANDS: dict[str, dict[str, Any]] = {
    "host_info": {"desc": "Show local network interfaces/addresses", "argv": ["ipconfig", "/all"] if WIN else ["ip", "addr"],
                  "alt": [["ifconfig"]], "effects": ["read"], "risk": "low", "timeout": 15},
    "listening_ports": {"desc": "List listening TCP/UDP ports on this machine", "argv": ["netstat", "-ano"] if WIN else ["ss", "-tuln"],
                        "effects": ["read"], "risk": "low", "timeout": 15},
    "python_version": {"desc": "Show the Python interpreter version", "argv": [sys.executable, "--version"], "effects": ["read"], "risk": "low", "timeout": 10},
    "nslookup": {"desc": "DNS lookup of a hostname", "argv": ["nslookup", "{name}"], "params": {"name": _HOST}, "effects": ["network_read"],
                 "risk": "low", "timeout": 20},
    "ping": {"desc": "ICMP echo to an authorized host (count 1-10)", "argv": ["ping", "-n", "{count}", "{host}"] if WIN else ["ping", "-c", "{count}", "-W", "2", "{host}"],
             "params": {"host": _HOST, "count": r"([1-9]|10)"}, "target_param": "host", "effects": ["network_write"], "risk": "medium", "timeout": 40},
    "traceroute": {"desc": "Trace the network path to an authorized host", "argv": ["tracert", "-h", "15", "{host}"] if WIN else ["traceroute", "-m", "15", "{host}"],
                   "params": {"host": _HOST}, "target_param": "host", "effects": ["network_write"], "risk": "medium", "timeout": 90},
    "run_tests": {"desc": "Run pytest in an allowed project folder", "argv": [sys.executable, "-m", "pytest", "-q", "-x", "{path}"],
                  "params": {"path": r"[^\x00]{1,400}"}, "path_param": "path", "effects": ["exec"], "risk": "high", "timeout": 600},
}


def _commands(ctx_app) -> dict[str, dict]:
    cmds = dict(COMMANDS)
    for t in ctx_app.settings.external_tools:   # registered by the human in Settings; the model cannot add to this list
        try:
            cmds[t["name"]] = {"desc": t.get("description", "External utility"), "argv": [t["executable"], *t.get("args", [])],
                               "params": t.get("params", {}), "target_param": t.get("target_param"), "effects": t.get("effects", ["exec"]),
                               "risk": t.get("risk", "high"), "timeout": int(t.get("timeout", 120)), "external": True}
        except KeyError:
            continue
    return cmds


def _resolve_command(app, a: dict) -> tuple[list[str], dict]:
    cmds = _commands(app)
    spec = cmds.get(a["command"])
    if not spec:
        raise PermissionDenied(f"'{a['command']}' is not a registered command. Registered: {', '.join(sorted(cmds))}")
    params = a.get("params") or {}
    allowed = spec.get("params", {})
    for k in params:
        if k not in allowed:
            raise PermissionDenied(f"unexpected parameter '{k}'")
    argv = []
    for part in spec["argv"]:
        m = re.fullmatch(r"\{(\w+)\}", part)
        if m:
            k = m.group(1)
            v = str(params.get(k, ""))
            if not v or not re.fullmatch(allowed.get(k, r"$^"), v) or v.startswith("-") or any(c in v for c in "\n\r\x00"):
                raise PermissionDenied(f"parameter '{k}' is missing or invalid")
            if spec.get("path_param") == k:
                v = str(app.perms.paths.resolve(v))
            argv.append(v)
        else:
            argv.append(part)
    return argv, spec


def _cmd_effects(ctx: ToolContext, a: dict) -> set[str]:
    _, spec = _resolve_command(ctx.app, a)
    return set(spec["effects"])


def _cmd_targets(app):
    def fn(a: dict) -> list[str]:
        try:
            _, spec = _resolve_command(app, a)
        except PermissionDenied:
            return []
        tp = spec.get("target_param")
        host = str((a.get("params") or {}).get(tp, "")) if tp else ""
        return [host] if host and host not in ("localhost", "127.0.0.1", "::1") else []
    return fn


def run_command(ctx: ToolContext, a: dict) -> dict:
    argv, spec = _resolve_command(ctx.app, a)
    exe = shutil.which(argv[0]) or (argv[0] if Path(argv[0]).exists() else None)
    if not exe:
        for alt in spec.get("alt", []):
            if shutil.which(alt[0]):
                argv, exe = alt, shutil.which(alt[0])
                break
    if not exe:
        return fail(f"executable '{argv[0]}' was not found on this machine")
    argv[0] = exe
    cwd = str(ctx.app.settings.path("workspace"))
    r = run_limited(argv, stdin=None, cwd=cwd, timeout=spec["timeout"], mem_mb=1024, out_limit=30000, cancelled=ctx.cancelled)
    if r["killed"]:
        return fail(f"command {r['killed']}", partial=r["stdout"][-2000:])
    return ok(command=a["command"], argv=[Path(argv[0]).name, *argv[1:]], returncode=r["returncode"], stdout=r["stdout"], stderr=r["stderr"],
              seconds=r["seconds"])


# ---------------------------------------------------------------- read-only SQL on the app database
def db_query(ctx: ToolContext, a: dict) -> dict:
    sql = a["sql"].strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", sql) or ";" in sql:
        return fail("only a single SELECT statement is allowed")
    allowed = {"conversations", "messages", "collections", "documents", "chunks", "memories", "ingestion_jobs", "ingestion_items",
               "ingestion_rejects", "tasks", "task_events", "tool_runs", "skills", "facts", "feedback", "eval_cases", "eval_runs",
               "research_jobs", "research_pages", "dataset_profiles"}
    path = ctx.app.settings.db_path
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row

    def auth(action, arg1, arg2, dbname, src):
        if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_FUNCTION or action == sqlite3.SQLITE_RECURSIVE:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if arg1 in allowed or arg1.endswith("_fts") else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
    c.execute("PRAGMA query_only=ON")
    c.set_authorizer(auth)
    try:
        cur = c.execute(sql)
        rows = [dict(r) for r in cur.fetchmany(int(a.get("limit", 100)) + 1)]
    except sqlite3.Error as e:
        return fail(f"SQL error: {e}")
    finally:
        c.close()
    lim = int(a.get("limit", 100))
    return ok(rows=[{k: (v[:300] + "..." if isinstance(v, str) and len(v) > 300 else v) for k, v in r.items()} for r in rows[:lim]],
              truncated=len(rows) > lim)


# ---------------------------------------------------------------- reports
def report_generate(ctx: ToolContext, a: dict) -> dict:
    fmt = a.get("format", "md")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", a.get("filename") or f"report_{int(time.time())}")
    name = name if name.endswith("." + fmt) else f"{name}.{fmt}"
    p = ctx.app.settings.path("reports", name)
    if fmt == "md":
        body = f"# {a['title']}\n\n_Generated {time.strftime('%Y-%m-%d %H:%M:%S')} by NEXUS AI_\n\n"
        for sec in a.get("sections", []):
            body += f"## {sec.get('heading', '')}\n\n{sec.get('body', '')}\n\n"
        if a.get("markdown"):
            body += a["markdown"]
        p.write_text(body, encoding="utf-8")
    elif fmt == "json":
        p.write_text(json.dumps({"title": a["title"], "sections": a.get("sections", []), "data": a.get("data")}, indent=2, default=str), encoding="utf-8")
    elif fmt == "csv":
        rows = a.get("rows") or []
        if not rows:
            return fail("csv format needs 'rows' (list of objects)")
        cols = list(dict.fromkeys(k for r in rows for k in r))
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
        p.write_text(buf.getvalue(), encoding="utf-8", newline="")
    else:
        return fail("format must be md, json or csv")
    return ok(path=str(p), bytes=p.stat().st_size, sha256=hashlib.sha256(p.read_bytes()).hexdigest())


def _verify_report(ctx: ToolContext, a: dict, r: dict) -> tuple[bool, str]:
    p = Path(r["path"])
    return p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == r["sha256"], "file exists and its SHA-256 matches"


# ---------------------------------------------------------------- diagnostics
def system_diagnostics(ctx: ToolContext, a: dict) -> dict:
    hw = ctx.app.hardware()
    top = []
    for p in sorted(psutil.process_iter(["name", "memory_info"]), key=lambda p: -(p.info["memory_info"].rss if p.info.get("memory_info") else 0))[:5]:
        top.append({"name": p.info["name"], "rss_mb": (p.info["memory_info"].rss // 2**20) if p.info.get("memory_info") else None})
    return ok(hardware=hw, top_memory_processes=top, lm_studio=ctx.app.provider.health(), execution_mode=ctx.app.settings.execution_mode,
              python=sys.version.split()[0], platform=platform.platform())


# ---------------------------------------------------------------- configured API integrations
def _integration(app, name: str) -> dict:
    for i in app.settings.integrations:
        if i.get("name") == name:
            return i
    raise PermissionDenied(f"integration '{name}' is not configured. A human can add it under Settings > Integrations.")


def _api_effects(ctx: ToolContext, a: dict) -> set[str]:
    integ = _integration(ctx.app, a["integration"])
    method = a.get("method", "GET").upper()
    if method not in ("GET", "HEAD"):
        if not integ.get("allow_write"):
            raise PermissionDenied(f"integration '{a['integration']}' is read-only (GET) - a human must enable writes")
        return {NET_WRITE}
    return {NET_READ}


def _api_targets(app):
    def fn(a: dict) -> list[str]:
        return []
    return fn


def api_call(ctx: ToolContext, a: dict) -> dict:
    import httpx
    from ...web_research.fetcher import UrlPolicy
    integ = _integration(ctx.app, a["integration"])
    base = integ["base_url"].rstrip("/")
    path = "/" + a.get("path", "").lstrip("/")
    url = base + path
    UrlPolicy(ctx.app.settings).check(url, explicit_hosts=[httpx.URL(base).host])
    headers = {"User-Agent": ctx.app.settings.web_user_agent}
    if integ.get("auth_env") and os.environ.get(integ["auth_env"]):
        headers[integ.get("auth_header", "Authorization")] = f"{integ.get('auth_prefix', 'Bearer ')}{os.environ[integ['auth_env']]}"
    r = httpx.request(a.get("method", "GET").upper(), url, params=a.get("params"), json=a.get("body"), headers=headers, timeout=30, follow_redirects=False)
    try:
        body: Any = r.json() if "json" in r.headers.get("content-type", "") else r.text
    except ValueError:
        body = r.text
    text, trunc = truncate(body if isinstance(body, str) else json.dumps(body, default=str), 8000)
    return ok(status=r.status_code, body=text, truncated=trunc)


def register(reg: ToolRegistry, app) -> None:
    S = lambda **p: {"type": "object", "properties": p, "additionalProperties": False}  # noqa: E731
    reg.register(Tool("python_sandbox", "Run short Python (stdlib math/data only; no files, network or OS access) in an isolated child process. "
                      "Pass inputs via `data` (available as DATA); set RESULT to return a value.",
                      {**S(code={"type": "string"}, data={}, timeout={"type": "integer"}), "required": ["code"]},
                      python_sandbox, effects={EXEC_SANDBOX}, risk="high", timeout=90,
                      docs="Best-effort isolation: AST allow-list + restricted builtins + OS rlimits (POSIX) + wall/memory watchdog. "
                           "Not a security boundary against a determined attacker; every run needs approval unless you add it to the auto-allow list."))
    reg.register(Tool("run_command", "Run a REGISTERED diagnostic command (no shell). Use params for arguments. Registered: "
                      + ", ".join(sorted(COMMANDS)) + " plus any external utilities a human registered.",
                      {**S(command={"type": "string"}, params={"type": "object"}), "required": ["command"]},
                      run_command, effects=_cmd_effects, risk="medium", timeout=700, targets_fn=_cmd_targets(app)))
    reg.register(Tool("db_query", "Run a read-only SELECT against the application's database (conversations, documents, tasks, memories...).",
                      {**S(sql={"type": "string"}, limit={"type": "integer"}), "required": ["sql"]}, db_query))
    reg.register(Tool("report_generate", "Write a Markdown, JSON or CSV report file into the reports folder.",
                      {**S(title={"type": "string"}, format={"type": "string", "enum": ["md", "json", "csv"]}, filename={"type": "string"},
                           sections={"type": "array"}, markdown={"type": "string"}, rows={"type": "array"}, data={}), "required": ["title"]},
                      report_generate, effects={SCRATCH}, risk="low", verify_fn=_verify_report))
    reg.register(Tool("system_diagnostics", "Report hardware, disk, memory, top processes and LM Studio connection status.",
                      S(), system_diagnostics, dependencies=["psutil"]))
    reg.register(Tool("api_call", "Call a configured API integration (GET by default). Integrations are defined by the human in Settings.",
                      {**S(integration={"type": "string"}, method={"type": "string"}, path={"type": "string"}, params={"type": "object"}, body={}),
                       "required": ["integration"]},
                      api_call, effects=_api_effects, risk="medium", untrusted_output=True))
