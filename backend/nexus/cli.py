"""Command-line interface:  python -m nexus.cli <command>"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path


def _ctx():
    from .core import AppContext
    return AppContext()


def cmd_serve(a) -> int:
    import uvicorn
    from .api.app import create_app
    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="info")
    return 0


def cmd_init_db(a) -> int:
    c = _ctx()
    print(f"Database ready at {c.db.path} (schema v{c.db.scalar('SELECT value FROM meta WHERE key=?', ('schema_version',))})")
    return 0


def cmd_doctor(a) -> int:
    from .api import setup_wizard
    c = _ctx()
    bad = 0
    for sid, title, _ in setup_wizard.STEPS[:10]:
        r = setup_wizard.run_step(c, sid, {})
        print(f"[{'OK' if r['ok'] else 'FAIL'}] {title}: {r['detail']}")
        if not r["ok"]:
            bad += 1
            if r.get("hint"):
                print(f"       -> {r['hint']}")
    return 1 if bad else 0


def cmd_ingest(a) -> int:
    c = _ctx()
    job = c.ingestion.create_job(a.name or "cli-ingest", a.collection, a.paths)
    print(f"Job {job['id']}: {job['counts']['items_discovered']} item(s) discovered")
    c.ingestion.run(job["id"], force_quality=a.force)
    print(c.ingestion.report(job["id"], with_retrieval_check=True)["markdown"])
    return 0


def cmd_dataset(a) -> int:
    c = _ctx()
    if a.action == "inspect":
        print(json.dumps(c.datasets.inspect(a.ref), indent=2))
    elif a.action == "plan":
        print(json.dumps(c.datasets.plan(a.ref, a.files, a.splits), indent=2, default=str))
    elif a.action == "download":
        d = c.datasets.start_download(a.ref, a.files, a.splits, a.approve_large, background=False)
        d = c.datasets.get(d["id"])
        print(json.dumps({k: d[k] for k in ("id", "status", "dest", "error")}, indent=2))
    elif a.action == "import":
        d = c.datasets.get(a.ref)
        job = c.datasets.import_dataset(a.ref, a.collection, start=False)
        c.ingestion.run(job["id"])
        print(c.ingestion.report(job["id"])["markdown"])
    return 0


def cmd_backup(a) -> int:
    c = _ctx()
    dest = c.settings.path("backups", "cli-backup.zip")
    tmp = c.settings.path("backups", "_db.sqlite")
    c.db.backup_to(tmp)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "nexus.db")
        sj = Path(c.settings.data_dir) / "settings.json"
        if sj.exists():
            z.write(sj, "settings.json")
    tmp.unlink()
    print(dest)
    return 0


def cmd_restore(a) -> int:
    from .config import load_settings
    s = load_settings()
    zp = Path(a.zip)
    if not zp.is_file():
        print("backup file not found")
        return 1
    with zipfile.ZipFile(zp) as z:
        names = set(z.namelist())
        keep = s.path("backups", "pre-restore.sqlite")
        if s.db_path.exists():
            shutil.copy2(s.db_path, keep)
        for wal in ("nexus.db-wal", "nexus.db-shm"):
            s.path(wal).unlink(missing_ok=True)
        z.extract("nexus.db", s.data_dir)
        if "settings.json" in names:
            z.extract("settings.json", s.data_dir)
    print(f"Restored. Previous database kept at {keep}. Start NEXUS and run Knowledge > Re-index for each collection.")
    return 0


def cmd_test(a) -> int:
    import pytest
    return pytest.main([str(Path(__file__).resolve().parents[1] / "tests"), "-q", *a.args])


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nexus", description="NEXUS AI command line")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the API (and the built web UI if frontend/dist exists)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("init-db", help="create/upgrade the SQLite database").set_defaults(fn=cmd_init_db)
    sub.add_parser("doctor", help="run the first 10 setup checks in the console").set_defaults(fn=cmd_doctor)
    i = sub.add_parser("ingest", help="ingest files/folders into a collection")
    i.add_argument("paths", nargs="+")
    i.add_argument("--collection", default="default")
    i.add_argument("--name", default="")
    i.add_argument("--force", action="store_true", help="ignore the quality gate")
    i.set_defaults(fn=cmd_ingest)
    d = sub.add_parser("dataset", help="inspect / plan / download / import a dataset")
    d.add_argument("action", choices=["inspect", "plan", "download", "import"])
    d.add_argument("ref", help="HF repo id (owner/name), URL, local path, or (for import) a download id")
    d.add_argument("--files", nargs="*")
    d.add_argument("--splits", nargs="*")
    d.add_argument("--approve-large", action="store_true")
    d.add_argument("--collection", default="datasets")
    d.set_defaults(fn=cmd_dataset)
    sub.add_parser("backup", help="write a backup zip of the database and settings").set_defaults(fn=cmd_backup)
    r = sub.add_parser("restore", help="restore a backup zip (stop NEXUS first)")
    r.add_argument("zip")
    r.set_defaults(fn=cmd_restore)
    t = sub.add_parser("test", help="run the automated test-suite")
    t.add_argument("args", nargs="*")
    t.set_defaults(fn=cmd_test)
    a = p.parse_args(argv)
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
