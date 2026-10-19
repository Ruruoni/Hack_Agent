"""First-run setup wizard: 15 verifiable steps. Each step returns {ok, detail, hint}; progress is stored so the UI can resume."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from typing import Any

from ..config import save_overrides
from ..database.db import now
from ..llm.base import LLMError

SAMPLE_DOC = """Project Nimbus - Operations Notes

Project Nimbus is the internal name for the telemetry gateway. The Nimbus gateway listens on TCP port 7421 and
forwards metrics to the collector cluster. Certificates for Nimbus are rotated every 90 days by the platform team.
If the gateway stops responding, restart the nimbus-gateway service and check the health endpoint at /healthz.
"""

SAMPLE_CSV = "ticket_id,category,priority,summary\n" + "\n".join(
    f"{1000 + i},{['network', 'storage', 'access', 'printing'][i % 4]},{['low', 'medium', 'high'][i % 3]},Sample ticket {i} about {['vpn drops', 'disk quota', 'password reset', 'toner empty'][i % 4]} on floor {i % 7}"
    for i in range(60)) + "\nbroken,row\n"

STEPS = [
    ("python", "Python installation", "Checks that the Python running this backend is 3.10 or newer."),
    ("node", "Node.js installation", "Needed only to run/build the web interface (npm)."),
    ("directories", "Project directories", "Creates the data folders and checks they are writable."),
    ("lmstudio", "Connect to LM Studio", "Tests the local LM Studio server."),
    ("models", "Discover downloaded models", "Lists the models LM Studio reports."),
    ("select_model", "Select the active model", "Chooses a chat model (you can change it later in Models)."),
    ("embedding", "Embedding model", "Tests the embedding provider; falls back to the built-in lexical embedder."),
    ("database", "Create the application database", "Initialises SQLite and checks integrity."),
    ("knowledge_base", "Initialise the knowledge base", "Creates the vector store and a sample collection."),
    ("model_test", "Test model responses", "Sends a tiny prompt to the selected model."),
    ("sample_document", "Upload a sample document", "Ingests a small bundled document."),
    ("retrieval", "Verify semantic retrieval", "Asks a question that only the sample document can answer."),
    ("sample_dataset", "Run a small dataset ingestion", "Ingests a bundled 60-row CSV (including one deliberately broken row)."),
    ("web_import", "Test a public webpage import", "Fetches a URL you choose (default https://example.com) and indexes it."),
    ("memory_persist", "Verify memory persists after restart", "Writes a marker memory; restart the app; run the step again with verify."),
]


def _state(ctx) -> dict:
    row = ctx.db.one("SELECT value FROM meta WHERE key='wizard'")
    return json.loads(row["value"]) if row else {}


def _save(ctx, st: dict) -> None:
    ctx.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('wizard',?)", (json.dumps(st),))


def list_steps(ctx) -> list[dict]:
    st = _state(ctx)
    return [{"id": i, "title": t, "description": d, **({"last": st[i]} if i in st else {})} for i, t, d in STEPS]


def run_step(ctx, step: str, body: dict) -> dict:
    fn = globals().get(f"_step_{step}")
    if not fn:
        raise ValueError(f"unknown step '{step}'")
    t0 = time.time()
    try:
        res = fn(ctx, body)
    except LLMError as e:
        res = {"ok": False, "detail": e.message, "hint": e.hint}
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "detail": f"{e.__class__.__name__}: {e}", "hint": "See docs/TROUBLESHOOTING.md"}
    res["seconds"] = round(time.time() - t0, 2)
    st = _state(ctx)
    st[step] = {"ok": res["ok"], "ts": now(), "detail": res.get("detail", "")}
    _save(ctx, st)
    return res


def _ok(detail: str, **kw: Any) -> dict:
    return {"ok": True, "detail": detail, **kw}


def _bad(detail: str, hint: str = "", **kw: Any) -> dict:
    return {"ok": False, "detail": detail, "hint": hint, **kw}


def _step_python(ctx, b):
    v = sys.version_info
    if v < (3, 10):
        return _bad(f"Python {v.major}.{v.minor} is too old", "Install Python 3.11+ from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH').")
    return _ok(f"Python {v.major}.{v.minor}.{v.micro} at {sys.executable}")


def _step_node(ctx, b):
    exe = shutil.which("node")
    if not exe:
        return _bad("Node.js not found on PATH", "Install the LTS version from https://nodejs.org/ then reopen PowerShell and run `node --version`. (Only needed for the web UI.)")
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    major = int(out.lstrip("v").split(".")[0] or 0)
    if major < 18:
        return _bad(f"Node {out} is too old (need 18+)", "Install the current LTS from https://nodejs.org/")
    npm = shutil.which("npm")
    return _ok(f"Node {out}, npm {'found' if npm else 'NOT found'}")


def _step_directories(ctx, b):
    ctx.settings.ensure_dirs()
    probe = ctx.settings.path("workspace", ".write_test")
    probe.write_text("ok")
    probe.unlink()
    return _ok(f"Data directory: {ctx.settings.data_dir}", dirs=sorted(p.name for p in ctx.settings.path().iterdir() if p.is_dir()))


def _step_lmstudio(ctx, b):
    h = ctx.provider.health()
    if h.get("ok"):
        return _ok(f"Connected to {h['base_url']} ({h['models']} model entries)")
    return _bad(h.get("message", "cannot connect"), h.get("hint", ""))


def _step_models(ctx, b):
    ms = ctx.provider.list_models()
    chat = [m for m in ms if m.type != "embedding"]
    emb = [m for m in ms if m.type == "embedding"]
    if not chat:
        return _bad("LM Studio reports no chat models", "In LM Studio open the Discover tab, download a model (e.g. a 7-8B instruct model), then load it in the Developer tab.")
    return _ok(f"{len(chat)} chat model(s), {len(emb)} embedding model(s)", chat=[m.id for m in chat], embedding=[m.id for m in emb], loaded=[m.id for m in chat if m.loaded])


def _step_select_model(ctx, b):
    chosen = b.get("model") or ctx.settings.chat_model
    if not chosen:
        ms = [m for m in ctx.provider.list_models() if m.type != "embedding"]
        pick = next((m for m in ms if m.loaded), ms[0] if ms else None)
        if not pick:
            return _bad("no model to select", "Download and load a model in LM Studio first.")
        chosen = pick.id
    save_overrides(ctx.settings, {"chat_model": chosen})
    caps = ctx.provider.capabilities(chosen)  # type: ignore[attr-defined]
    return _ok(f"Active model: {chosen}", capabilities=caps.to_dict())


def _step_embedding(ctx, b):
    if b.get("provider"):
        save_overrides(ctx.settings, {"embedding_provider": b["provider"], **({"embedding_model": b["model"]} if b.get("model") else {})})
    e = ctx.embedder
    try:
        v = e.embed(["wizard embedding test"])
    except LLMError as err:
        save_overrides(ctx.settings, {"embedding_provider": "hash"})
        return _bad(f"Embedding model failed ({err.message}); switched to the built-in lexical embedder", err.hint or "Load an embedding model (e.g. nomic-embed-text) in LM Studio, then choose provider 'lmstudio'.",
                    fallback="hash")
    kind = "semantic (LM Studio)" if ctx.settings.embedding_provider == "lmstudio" else "lexical hash fallback - works offline, but not truly semantic"
    return _ok(f"Embedder {e.id}, dim {v.shape[1]} - {kind}")


def _step_database(ctx, b):
    ctx.db.init()
    ic = ctx.db.scalar("PRAGMA integrity_check", (), "?")
    tables = ctx.db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table'", (), 0)
    return _ok(f"SQLite at {ctx.db.path}: integrity={ic}, {tables} tables") if ic == "ok" else _bad(f"integrity check: {ic}")


def _step_knowledge_base(ctx, b):
    c = ctx.kb.create_collection("wizard-samples", "Created by the setup wizard")
    return _ok(f"Vector backend '{ctx.vectors.name}'; collection '{c['name']}' ready")


def _step_model_test(ctx, b):
    t0 = time.time()
    r = ctx.provider.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=40, temperature=0)
    return _ok(f"Model answered in {time.time() - t0:.1f}s: {r.text.strip()[:80]!r}", model=r.model)


def _step_sample_document(ctx, b):
    p = ctx.settings.path("uploads", "wizard_nimbus_notes.txt")
    p.write_text(SAMPLE_DOC, encoding="utf-8")
    job = ctx.ingestion.create_job("wizard sample document", "wizard-samples", [str(p)])
    ctx.ingestion.run(job["id"])
    j = ctx.ingestion.get_job(job["id"])
    if j["counts"]["chunks_indexed"] < 1:
        return _bad(f"nothing indexed ({j['status']}): {j.get('error') or j['counts']}")
    return _ok(f"Indexed {j['counts']['chunks_indexed']} chunk(s) from the sample document", job=job["id"])


def _step_retrieval(ctx, b):
    res = ctx.kb.search("Which TCP port does the Nimbus gateway listen on?", None, k=3)
    top = res["hits"][0] if res["hits"] else None
    if top and "7421" in top["text"]:
        return _ok(f"Top hit (score {top['score']}) from {top['source'].split('/')[-1]} contains the answer: port 7421", degraded=res["degraded"])
    return _bad("The sample answer was not retrieved", "Run the 'sample_document' step first; if you changed the embedding model, re-index the collection.", degraded=res["degraded"])


def _step_sample_dataset(ctx, b):
    p = ctx.settings.path("uploads", "wizard_tickets.csv")
    p.write_text(SAMPLE_CSV, encoding="utf-8")
    job = ctx.ingestion.create_job("wizard sample dataset", "wizard-samples", [str(p)])
    ctx.ingestion.run(job["id"])
    c = ctx.ingestion.get_job(job["id"])["counts"]
    return _ok(f"{c['records_seen']} records read, {c['chunks_indexed']} chunks indexed, {c['records_rejected']} malformed row(s) rejected and reported", counts=c, job=job["id"])


def _step_web_import(ctx, b):
    url = b.get("url") or "https://example.com"
    job = ctx.ingestion.create_job("wizard web import", "wizard-samples", [url])
    ctx.ingestion.run(job["id"])
    j = ctx.ingestion.get_job(job["id"])
    items = ctx.ingestion.list_items(job["id"])
    if j["counts"]["chunks_indexed"] >= 1:
        return _ok(f"Imported {url}: {j['counts']['chunks_indexed']} chunk(s); source URL preserved", job=job["id"])
    reason = items[0]["error"] or items[0]["reason"] if items else "unknown"
    return _bad(f"Could not import {url}: {reason}", "Check your internet connection / proxy and the Web allow/deny lists in Settings. You can try another public URL.")


def _step_memory_persist(ctx, b):
    marker = ctx.db.one("SELECT * FROM memories WHERE source_type='wizard' ORDER BY created DESC LIMIT 1")
    if b.get("verify"):
        if not marker:
            return _bad("No marker memory found", "Run this step once (without verify), restart NEXUS, then run it again with verify.")
        if marker["meta"].get("boot_id") == ctx.boot_id:
            return _bad("The app has not been restarted since the marker was written", "Stop NEXUS (Ctrl+C or stop.ps1), start it again, then verify.")
        found = ctx.memory.search("wizard persistence marker", k=3)
        if any(m["id"] == marker["id"] for m in found):
            return _ok("The marker memory survived the restart and is retrievable")
        return _bad("Marker exists in the database but was not retrievable", "Try Memory > Re-index.")
    m = ctx.memory.add("project", f"wizard persistence marker written at {time.strftime('%Y-%m-%d %H:%M:%S')}", kind="note", trust="user_verified",
                       source_type="wizard", meta={"boot_id": ctx.boot_id})
    return _ok("Marker memory written. Now restart NEXUS and run this step again with verify=true.", memory=m["id"], next="restart")
