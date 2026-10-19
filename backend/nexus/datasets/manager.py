"""Dataset manager: inspect -> plan (size, disk, licence, access) -> approved download -> verify -> preview -> import.

* Hugging Face datasets via `huggingface_hub` (token ONLY from the HF_TOKEN / HUGGINGFACE_HUB_TOKEN environment variable).
* Direct URLs via a resumable, SSRF-guarded streaming downloader.
* Local files/folders are registered in place (never copied or modified).
Nothing is downloaded without an explicit `start_download` call; anything larger than `max_auto_download_mb`
additionally needs `approve_large=True`.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urljoin, urlparse

import httpx

from ..config import Settings
from ..database.db import Database, new_id, now
from ..ingestion import parsers
from ..system.hardware import check_disk
from ..web_research.fetcher import UrlPolicy, WebPolicyError

HF_RX = re.compile(r"^(?:hf://datasets/|https?://huggingface\.co/datasets/)?([A-Za-z0-9][\w.\-]*(?:/[\w.\-]+)?)/?$")
DATA_EXT = (".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet", ".txt", ".md", ".xlsx", ".xml", ".zip", ".gz", ".tar", ".tgz")
SPLIT_RX = re.compile(r"(train|test|val|valid|validation|dev)", re.I)


class DatasetError(Exception):
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint}


def hf_token() -> str | None:
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN") or None


def _safe_name(name: str) -> str:
    n = unquote(name).replace("\\", "/").split("/")[-1]
    n = re.sub(r"[^A-Za-z0-9._\- ]", "_", n).strip(" .")
    return n[:150] or "download.bin"


class DatasetManager:
    def __init__(self, settings: Settings, db: Database, ingestion, hf_api_factory: Callable[[], Any] | None = None,
                 transport: httpx.BaseTransport | None = None, on_event=None, hf_download: Callable[..., str] | None = None):
        self.s, self.db, self.ingestion = settings, db, ingestion
        self._hf_factory = hf_api_factory
        self._hf_download = hf_download
        self._transport = transport
        self.on_event = on_event or (lambda k, d: None)
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------ inspect
    def classify(self, ref: str) -> str:
        if ref.startswith(("http://", "https://")) and "huggingface.co/datasets/" not in ref:
            return "url"
        if Path(ref).expanduser().exists():
            return "local"
        if HF_RX.match(ref):
            return "huggingface"
        raise DatasetError("bad_reference", f"'{ref}' is not an existing path, a URL, or a Hugging Face dataset id (owner/name).")

    def inspect(self, ref: str) -> dict:
        kind = self.classify(ref)
        if kind == "local":
            return self._inspect_local(ref)
        if kind == "url":
            return self._inspect_url(ref)
        return self._inspect_hf(ref)

    def _inspect_local(self, ref: str) -> dict:
        p = Path(ref).expanduser().resolve()
        files = [p] if p.is_file() else [f for f in sorted(p.rglob("*")) if f.is_file() and not any(x.startswith(".") for x in f.relative_to(p).parts)]
        out = [{"name": str(f.relative_to(p.parent if p.is_file() else p)), "size": f.stat().st_size, "supported": parsers.detect_kind(f) not in ("unsupported", "empty")} for f in files[:2000]]
        return {"kind": "local", "id": str(p), "files": out, "total_bytes": sum(f["size"] for f in out), "license": "unknown (local files)",
                "requires_auth": False, "warnings": [] if out else ["no files found"], "truncated": len(files) > 2000}

    def _inspect_url(self, ref: str) -> dict:
        try:
            UrlPolicy(self.s).check(ref)
        except WebPolicyError as e:
            raise DatasetError("blocked", str(e)) from e
        try:
            with httpx.Client(timeout=20, follow_redirects=True, transport=self._transport, headers={"User-Agent": self.s.web_user_agent}) as c:
                r = c.head(ref)
                if r.status_code >= 400 or "content-length" not in r.headers:
                    r = c.get(ref, headers={"Range": "bytes=0-0"})
        except httpx.HTTPError as e:
            raise DatasetError("network", f"cannot reach {ref}: {e}") from e
        if r.status_code in (401, 403):
            raise DatasetError("auth_required", f"HTTP {r.status_code}: this URL needs credentials", "Download it manually in your browser, then register the local file.")
        size = int(r.headers.get("content-range", "/0").split("/")[-1] or 0) if "content-range" in r.headers else int(r.headers.get("content-length", 0) or 0)
        name = _safe_name(urlparse(str(r.url)).path or "download.bin")
        return {"kind": "url", "id": ref, "files": [{"name": name, "size": size, "supported": name.lower().endswith(DATA_EXT)}], "total_bytes": size,
                "license": "unknown - check the source's terms before use", "requires_auth": False,
                "warnings": [] if size else ["server did not report a size"], "content_type": r.headers.get("content-type", "")}

    def _hf(self):
        if self._hf_factory:
            return self._hf_factory()
        try:
            from huggingface_hub import HfApi
        except ImportError as e:
            raise DatasetError("missing_dependency", "huggingface_hub is not installed", "pip install huggingface_hub") from e
        return HfApi(token=hf_token())

    def _inspect_hf(self, ref: str) -> dict:
        m = HF_RX.match(ref)
        repo = m.group(1) if m else ref
        api = self._hf()
        try:
            info = api.dataset_info(repo, files_metadata=True)
        except Exception as e:  # noqa: BLE001 - hub raises several error classes; map by name
            name = e.__class__.__name__
            if "Gated" in name:
                raise DatasetError("gated", f"'{repo}' is a gated dataset: you must accept its terms on huggingface.co and authenticate.",
                                   "Create a read token at https://huggingface.co/settings/tokens, set it as the HF_TOKEN environment variable "
                                   "(never paste it into chat), and restart the backend.") from e
            if "NotFound" in name or "404" in str(e):
                raise DatasetError("not_found", f"dataset '{repo}' was not found (or is private and needs HF_TOKEN).") from e
            raise DatasetError("network", f"Hugging Face lookup failed: {name}: {e}") from e
        card = getattr(info, "card_data", None) or getattr(info, "cardData", None) or {}
        lic = card.get("license") if isinstance(card, dict) else getattr(card, "license", None)
        files = []
        for sib in getattr(info, "siblings", []) or []:
            fn = sib.rfilename
            if fn.startswith(".") or fn.lower() in ("readme.md", ".gitattributes"):
                continue
            lfs = getattr(sib, "lfs", None)
            sha = (lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)) if lfs else None
            m2 = SPLIT_RX.search(fn)
            files.append({"name": fn, "size": getattr(sib, "size", None) or 0, "sha256": sha, "supported": fn.lower().endswith(DATA_EXT),
                          "split": m2.group(1).lower() if m2 else None})
        warns = []
        if getattr(info, "gated", False):
            warns.append("This dataset is gated: authentication is required and you must have accepted its terms.")
        if not lic:
            warns.append("No licence is declared on the dataset card - check permitted use before training or redistribution.")
        return {"kind": "huggingface", "id": repo, "license": lic or "not declared", "gated": bool(getattr(info, "gated", False)),
                "requires_auth": bool(getattr(info, "gated", False) or getattr(info, "private", False)), "files": files,
                "total_bytes": sum(f["size"] for f in files), "tags": list(getattr(info, "tags", []) or [])[:20],
                "downloads": getattr(info, "downloads", None), "warnings": warns, "token_configured": hf_token() is not None}

    # ------------------------------------------------------------------ plan
    def plan(self, ref: str, files: list[str] | None = None, splits: list[str] | None = None) -> dict:
        info = self.inspect(ref)
        sel = info["files"]
        if files:
            sel = [f for f in sel if f["name"] in files]
        elif splits:
            wanted = {x.lower() for x in splits}
            sel = [f for f in sel if f.get("split") in wanted]
        else:
            sel = [f for f in sel if f.get("supported")]
        total = sum(f["size"] for f in sel)
        ok, free = check_disk(self.s.path("downloads"), total, self.s.min_free_disk_mb)
        needs_approval = total > self.s.max_auto_download_mb * 2**20
        return {"info": info, "selected": sel, "selected_bytes": total, "disk_free_bytes": free, "disk_ok": ok,
                "needs_explicit_approval": needs_approval,
                "access": ("authentication required" + ("" if info.get("token_configured") else " (no HF_TOKEN set)")) if info["requires_auth"] else "public",
                "license": info["license"], "warnings": info["warnings"] + ([] if ok else ["Not enough free disk space for this selection."])}

    # ------------------------------------------------------------------ download
    def start_download(self, ref: str, files: list[str] | None = None, splits: list[str] | None = None, approve_large: bool = False,
                       background: bool = True) -> dict:
        pl = self.plan(ref, files, splits)
        if not pl["selected"]:
            raise DatasetError("nothing_selected", "No files selected. Choose files or splits from the inspection result.")
        if not pl["disk_ok"]:
            raise DatasetError("disk_space", f"Not enough disk space: need {pl['selected_bytes'] // 2**20} MB, {pl['disk_free_bytes'] // 2**20} MB free "
                                              f"(reserve {self.s.min_free_disk_mb} MB).")
        if pl["needs_explicit_approval"] and not approve_large:
            raise DatasetError("approval_required", f"This download is {pl['selected_bytes'] // 2**20} MB which exceeds the automatic limit of "
                                                    f"{self.s.max_auto_download_mb} MB. Confirm explicitly to proceed.")
        kind = pl["info"]["kind"]
        did = new_id("dl_")
        dest = self.s.path("downloads", re.sub(r"[^\w.\-]", "_", pl["info"]["id"])[:80] + "_" + did[-6:])
        if kind == "local":
            dest = Path(pl["info"]["id"])
        else:
            dest.mkdir(parents=True, exist_ok=True)
        self.db.insert("downloads", {"id": did, "kind": kind, "ref": ref, "status": "queued" if kind != "local" else "ready", "dest": str(dest),
                                     "files": [{"name": f["name"], "size": f["size"], "sha256": f.get("sha256"), "status": "pending"} for f in pl["selected"]],
                                     "total_bytes": pl["selected_bytes"], "done_bytes": 0, "meta": {"license": pl["license"], "access": pl["access"]},
                                     "created": now(), "updated": now()})
        self.db.audit("user", "dataset.download", {"id": did, "ref": ref, "bytes": pl["selected_bytes"], "kind": kind})
        if kind == "local":
            return self.get(did)  # type: ignore[return-value]
        self._cancel[did] = threading.Event()
        if background:
            t = threading.Thread(target=self._download, args=(did,), daemon=True, name=f"dl-{did}")
            self._threads[did] = t
            t.start()
        else:
            self._download(did)
        return self.get(did)  # type: ignore[return-value]

    def get(self, did: str) -> dict | None:
        return self.db.get("downloads", did)

    def list(self, limit: int = 50) -> list[dict]:
        return self.db.query("SELECT * FROM downloads ORDER BY created DESC LIMIT ?", (limit,))

    def wait(self, did: str, timeout: float | None = None) -> None:
        if did in self._threads:
            self._threads[did].join(timeout)

    def cancel(self, did: str) -> None:
        if did in self._cancel:
            self._cancel[did].set()

    def _update_file(self, did: str, name: str, **ch: Any) -> None:
        d = self.get(did)
        files = d["files"]
        for f in files:
            if f["name"] == name:
                f.update(ch)
        self.db.update("downloads", did, {"files": files, "done_bytes": sum(f.get("done", 0) for f in files), "updated": now()})

    def _download(self, did: str) -> None:
        d = self.get(did)
        self.db.update("downloads", did, {"status": "downloading", "updated": now()})
        cancel = self._cancel[did]
        try:
            for f in d["files"]:
                if cancel.is_set():
                    self.db.update("downloads", did, {"status": "cancelled", "updated": now()})
                    return
                if d["kind"] == "huggingface":
                    self._dl_hf(did, d, f)
                else:
                    self._dl_url(did, d, f, cancel)
            self.db.update("downloads", did, {"status": "verifying", "updated": now()})
            problems = self.verify(did)
            self.db.update("downloads", did, {"status": "ready" if not problems else "failed",
                                              "error": "; ".join(problems), "updated": now()})
        except DatasetError as e:
            self.db.update("downloads", did, {"status": "failed", "error": e.message, "updated": now()})
        except Exception as e:  # noqa: BLE001
            self.db.update("downloads", did, {"status": "failed", "error": f"{e.__class__.__name__}: {e}", "updated": now()})

    def _dl_hf(self, did: str, d: dict, f: dict) -> None:
        fn = self._hf_download
        if fn is None:
            try:
                from huggingface_hub import hf_hub_download as fn  # type: ignore[no-redef]
            except ImportError as e:
                raise DatasetError("missing_dependency", "huggingface_hub is not installed") from e
        m = HF_RX.match(d["ref"])
        path = fn(repo_id=m.group(1) if m else d["ref"], filename=f["name"], repo_type="dataset", local_dir=d["dest"], token=hf_token())
        size = Path(path).stat().st_size
        self._update_file(did, f["name"], status="done", done=size, size=size)

    def _dl_url(self, did: str, d: dict, f: dict, cancel: threading.Event) -> None:
        url = d["ref"]
        name = _safe_name(f["name"])
        part = Path(d["dest"]) / (name + ".part")
        final = Path(d["dest"]) / name
        policy = UrlPolicy(self.s)
        h = hashlib.sha256()
        start = part.stat().st_size if part.exists() else 0       # resume a partial file
        if start:
            h.update(part.read_bytes())
        with httpx.Client(timeout=30, follow_redirects=False, transport=self._transport, headers={"User-Agent": self.s.web_user_agent}) as c:
            cur = url
            for _ in range(6):
                policy.check(cur)
                hdr = {"Range": f"bytes={start}-"} if start else {}
                with c.stream("GET", cur, headers=hdr) as r:
                    if r.is_redirect:
                        cur = urljoin(cur, r.headers["location"])
                        continue
                    if r.status_code in (401, 403):
                        raise DatasetError("auth_required", f"HTTP {r.status_code}: credentials required for this URL")
                    if r.status_code == 416:       # already complete
                        break
                    if r.status_code not in (200, 206):
                        raise DatasetError("http", f"HTTP {r.status_code} downloading {cur}")
                    if r.status_code == 200 and start:   # server ignored Range: restart cleanly
                        start, h = 0, hashlib.sha256()
                        part.unlink(missing_ok=True)
                    with open(part, "ab") as out:
                        done = start
                        for chunk in r.iter_bytes(1 << 20):
                            if cancel.is_set():
                                self.db.update("downloads", did, {"status": "cancelled", "updated": now()})
                                return
                            out.write(chunk)
                            h.update(chunk)
                            done += len(chunk)
                            self._update_file(did, f["name"], done=done, status="downloading")
                            if done > max(f.get("size") or 0, 1) * 1.05 + (50 << 20) and f.get("size"):
                                raise DatasetError("size_mismatch", "server sent far more data than announced; aborting")
                break
            else:
                raise DatasetError("redirects", "too many redirects")
        os.replace(part, final)
        self._update_file(did, f["name"], status="done", sha256_actual=h.hexdigest(), size=final.stat().st_size, done=final.stat().st_size)

    def verify(self, did: str) -> list[str]:
        d = self.get(did)
        problems = []
        for f in d["files"]:
            p = Path(d["dest"]) / (_safe_name(f["name"]) if d["kind"] == "url" else f["name"])
            if not p.is_file():
                problems.append(f"{f['name']}: missing")
                continue
            actual = p.stat().st_size
            if f.get("size") and d["kind"] != "huggingface" and actual != f["size"]:
                problems.append(f"{f['name']}: size {actual} != expected {f['size']}")
            if f.get("sha256"):
                hh = hashlib.sha256()
                with open(p, "rb") as fh:
                    while b := fh.read(1 << 20):
                        hh.update(b)
                if hh.hexdigest() != f["sha256"]:
                    problems.append(f"{f['name']}: SHA-256 mismatch (file corrupted or changed)")
        return problems

    # ------------------------------------------------------------------ schema / preview / import
    def files_of(self, did: str) -> list[Path]:
        d = self.get(did)
        if not d:
            raise DatasetError("not_found", "unknown dataset id")
        base = Path(d["dest"])
        if d["kind"] == "local":
            return [base] if base.is_file() else [f for f in sorted(base.rglob("*")) if f.is_file()]
        return [base / (_safe_name(f["name"]) if d["kind"] == "url" else f["name"]) for f in d["files"] if (base / (_safe_name(f["name"]) if d["kind"] == "url" else f["name"])).is_file()]

    def preview(self, path: str | Path, n: int = 5) -> dict:
        p = Path(path)
        kind = parsers.detect_kind(p)
        ext = p.suffix.lower()
        rows: list[dict] = []
        cols: list[str] = []
        if ext in (".csv", ".tsv"):
            with open(p, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                rd = csv.DictReader(fh, delimiter="\t" if ext == ".tsv" else ",")
                cols = rd.fieldnames or []
                for r in rd:
                    rows.append(r)
                    if len(rows) >= n:
                        break
        elif ext in (".jsonl", ".ndjson"):
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
                    if len(rows) >= n:
                        break
            cols = sorted({k for r in rows if isinstance(r, dict) for k in r})
        elif ext == ".json":
            import ijson
            with open(p, "rb") as fh:
                try:
                    for obj in ijson.items(fh, "item", use_float=True):
                        rows.append(obj)
                        if len(rows) >= n:
                            break
                except ijson.JSONError:
                    pass
            cols = sorted({k for r in rows if isinstance(r, dict) for k in r})
        elif ext == ".parquet":
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(str(p))
            cols = pf.schema_arrow.names
            for b in pf.iter_batches(batch_size=n):
                rows = b.to_pylist()[:n]
                break
            return {"file": str(p), "kind": kind, "columns": cols, "schema": {f.name: str(f.type) for f in pf.schema_arrow}, "num_rows": pf.metadata.num_rows, "rows": rows}
        else:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                rows = [{"line": i + 1, "text": next(fh, "").rstrip()[:300]} for i in range(n)]
            cols = ["line", "text"]
        return {"file": str(p), "kind": kind, "columns": cols, "rows": rows[:n]}

    def import_dataset(self, did: str, collection: str, config: dict | None = None, start: bool = True) -> dict:
        d = self.get(did)
        if not d or d["status"] not in ("ready",):
            raise DatasetError("not_ready", "dataset must be fully downloaded and verified before import")
        sources = [str(p) for p in self.files_of(did)]
        job = self.ingestion.create_job(f"dataset-{Path(d['ref']).name or d['ref']}", collection, sources, config)
        if start:
            self.ingestion.start(job["id"])
        return job
