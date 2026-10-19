"""Reproducible, resumable web research jobs: search -> fetch -> extract -> dedupe -> review -> export / add to knowledge."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import Settings
from ..database.db import Database, new_id, now
from ..knowledge.text import scan_injection
from .fetcher import Fetcher, FetchResult, UrlPolicy, WebPolicyError, normalize_url

log = logging.getLogger("nexus.research")


class SearchError(Exception):
    pass


def web_search(s: Settings, query: str, n: int = 8, transport: httpx.BaseTransport | None = None) -> list[dict]:
    prov = s.search_provider
    if prov == "none":
        raise SearchError("No search provider is configured. Set Search provider to 'searxng' (self-hosted) or 'brave' in Settings, "
                          "or give explicit URLs instead.")
    with httpx.Client(timeout=20, transport=transport, headers={"User-Agent": s.web_user_agent}) as c:
        try:
            if prov == "searxng":
                host = urlparse(s.search_url).hostname or ""
                UrlPolicy(s).check(s.search_url, explicit_hosts=[host] if host in ("localhost", "127.0.0.1") else None)
                r = c.get(s.search_url.rstrip("/") + "/search", params={"q": query, "format": "json"})
                r.raise_for_status()
                return [{"title": x.get("title", ""), "url": x["url"], "snippet": x.get("content", "")} for x in r.json().get("results", [])[:n]]
            if prov == "brave":
                if not s.search_api_key:
                    raise SearchError("Brave search needs an API key (Settings > Search API key, or NEXUS_SEARCH_API_KEY).")
                r = c.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": n},
                          headers={"X-Subscription-Token": s.search_api_key, "Accept": "application/json"})
                r.raise_for_status()
                return [{"title": x.get("title", ""), "url": x["url"], "snippet": x.get("description", "")} for x in r.json().get("web", {}).get("results", [])[:n]]
        except (httpx.HTTPError, ValueError, KeyError, WebPolicyError) as e:
            raise SearchError(f"search request failed: {e}") from e
    raise SearchError(f"unknown search provider '{prov}'")


class ResearchManager:
    def __init__(self, settings: Settings, db: Database, stop_event: threading.Event | None = None, transport: httpx.BaseTransport | None = None,
                 search_transport: httpx.BaseTransport | None = None, on_event=None):
        self.s, self.db = settings, db
        self.fetcher = Fetcher(settings, transport)
        self.stop_event = stop_event or threading.Event()
        self.search_transport = search_transport
        self.on_event = on_event or (lambda k, d: None)
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}

    # ---- jobs ------------------------------------------------------------------------
    def create_job(self, query: str = "", urls: list[str] | None = None, params: dict | None = None) -> dict:
        p = {"max_pages": self.s.web_max_pages, "max_depth": self.s.web_max_depth, "same_domain": True, "js": False,
             "link_pattern": "", "search_results": 5, **(params or {})}
        p["max_pages"] = min(int(p["max_pages"]), 500)
        jid = new_id("rs_")
        seeds = [{"url": normalize_url(u), "depth": 0} for u in (urls or [])]
        self.db.insert("research_jobs", {"id": jid, "query": query, "params": {**p, "urls": urls or []}, "status": "queued",
                                         "frontier": seeds, "counts": {"fetched": 0, "ok": 0, "blocked": 0, "error": 0, "duplicate": 0, "flagged": 0},
                                         "created": now(), "updated": now()})
        self.db.audit("user", "research.create", {"job": jid, "query": query, "urls": (urls or [])[:10], "params": p})
        return self.get_job(jid)  # type: ignore[return-value]

    def get_job(self, jid: str) -> dict | None:
        j = self.db.get("research_jobs", jid)
        if j:
            j["live"] = jid in self._threads and self._threads[jid].is_alive()
        return j

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return self.db.query("SELECT * FROM research_jobs ORDER BY created DESC LIMIT ?", (limit,))

    def start(self, jid: str) -> None:
        t = self._threads.get(jid)
        if t and t.is_alive():
            return
        t = threading.Thread(target=self.run, args=(jid,), daemon=True, name=f"research-{jid}")
        self._threads[jid] = t
        t.start()

    def wait(self, jid: str, timeout: float | None = None) -> None:
        if jid in self._threads:
            self._threads[jid].join(timeout)

    def cancel(self, jid: str) -> None:
        self._cancel.setdefault(jid, threading.Event()).set()

    def recover(self) -> None:
        self.db.execute("UPDATE research_jobs SET status='interrupted' WHERE status='running'")

    def run(self, jid: str) -> dict:
        j = self.db.get("research_jobs", jid)
        if not j:
            raise ValueError("no such research job")
        ev = self._cancel[jid] = threading.Event()
        p = j["params"]
        counts = j["counts"]
        frontier: list[dict] = j["frontier"]
        self.db.update("research_jobs", jid, {"status": "running", "error": "", "updated": now()})
        try:
            if j["query"] and not p.get("searched"):
                hits = web_search(self.s, j["query"], int(p.get("search_results", 5)), self.search_transport)
                p["search_hits"] = hits
                p["searched"] = True
                frontier += [{"url": normalize_url(h["url"]), "depth": 0} for h in hits]
                self.db.update("research_jobs", jid, {"params": p, "frontier": frontier})
            done_urls = {r["url"] for r in self.db.query("SELECT url FROM research_pages WHERE job_id=?", (jid,))}
            hashes = {r["content_hash"] for r in self.db.query("SELECT content_hash FROM research_pages WHERE job_id=? AND status='ok'", (jid,))}
            origin_hosts = {urlparse(f["url"]).netloc for f in frontier if f["depth"] == 0}
            link_rx = re.compile(p["link_pattern"]) if p.get("link_pattern") else None
            while frontier and counts["fetched"] < p["max_pages"]:
                if ev.is_set() or self.stop_event.is_set():
                    self.db.update("research_jobs", jid, {"status": "cancelled", "frontier": frontier, "counts": counts, "updated": now()})
                    return self.get_job(jid)  # type: ignore[return-value]
                item = frontier.pop(0)
                url = item["url"]
                if url in done_urls:
                    continue
                done_urls.add(url)
                r = self.fetcher.fetch(url, js=bool(p.get("js")), save_dir=self.s.path("web", jid))
                counts["fetched"] += 1
                flagged = scan_injection(r.text) if r.status == "ok" else []
                dup = r.status == "ok" and r.content_hash in hashes
                status = "duplicate" if dup else r.status
                if r.status == "ok":
                    hashes.add(r.content_hash)
                counts[status if status in counts else "error"] = counts.get(status if status in counts else "error", 0) + 1
                if flagged:
                    counts["flagged"] += 1
                    self.db.audit("system", "injection.detected", {"url": url, "patterns": flagged, "job": jid})
                self.db.insert("research_pages", {
                    "id": new_id("pg_"), "job_id": jid, "url": url, "final_url": r.final_url, "status": status, "http_status": r.http_status,
                    "title": r.title, "text": r.text, "content_hash": r.content_hash, "flagged": ",".join(flagged), "raw_path": r.raw_path,
                    "retrieved_at": r.retrieved_at, "depth": item["depth"], "approved": 0,
                    "meta": {"reason": r.reason, "headings": r.headings[:30], "links": len(r.links), "tables": r.tables[:3], "page_meta": r.meta,
                             "content_type": r.content_type, "link_sample": r.links[:20]}})
                self.on_event("research.page", {"job": jid, "url": url, "status": status})
                if r.status == "ok" and not dup and item["depth"] < p["max_depth"]:
                    for l in r.links:
                        lu = l["url"]
                        if lu in done_urls or any(f["url"] == lu for f in frontier):
                            continue
                        host = urlparse(lu).netloc
                        if p.get("same_domain") and host not in origin_hosts:
                            continue
                        if link_rx and not link_rx.search(lu):
                            continue
                        frontier.append({"url": lu, "depth": item["depth"] + 1})
                self.db.update("research_jobs", jid, {"frontier": frontier, "counts": counts, "updated": now()})
            final = "completed"
        except SearchError as e:
            self.db.update("research_jobs", jid, {"status": "failed", "error": str(e), "updated": now()})
            return self.get_job(jid)  # type: ignore[return-value]
        self.db.update("research_jobs", jid, {"status": final, "frontier": frontier, "counts": counts, "updated": now()})
        return self.get_job(jid)  # type: ignore[return-value]

    # ---- results & export ----------------------------------------------------------------
    def pages(self, jid: str, status: str | None = None, with_text: bool = False) -> list[dict]:
        cols = "id,job_id,url,final_url,status,http_status,title,flagged,retrieved_at,approved,depth,content_hash,meta,raw_path" + (",text" if with_text else ",substr(text,1,400) AS preview")
        q = f"SELECT {cols} FROM research_pages WHERE job_id=?" + (" AND status=?" if status else "") + " ORDER BY retrieved_at"
        return self.db.query(q, (jid, status) if status else (jid,))

    def approve(self, page_ids: list[str], approved: bool = True) -> int:
        if not page_ids:
            return 0
        ph = ",".join("?" * len(page_ids))
        return self.db.execute(f"UPDATE research_pages SET approved=? WHERE id IN ({ph}) AND status='ok'", (1 if approved else 0, *page_ids))

    def export(self, jid: str, fmt: str = "json") -> Path:
        j = self.get_job(jid)
        if not j:
            raise ValueError("no such job")
        rows = self.pages(jid, with_text=True)
        out = self.s.path("exports", f"research_{jid}.{fmt}")
        if fmt == "json":
            out.write_text(json.dumps({"job": {k: j[k] for k in ("id", "query", "params", "counts", "created")}, "pages": [
                {"url": r["url"], "final_url": r["final_url"], "status": r["status"], "title": r["title"], "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["retrieved_at"])),
                 "flagged": r["flagged"], "approved": bool(r["approved"]), "text": r["text"], "content_hash": r["content_hash"]} for r in rows]}, indent=2, ensure_ascii=False), encoding="utf-8")
        elif fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["url", "final_url", "status", "title", "retrieved_at", "flagged", "approved", "chars", "content_hash"])
            for r in rows:
                w.writerow([r["url"], r["final_url"], r["status"], r["title"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["retrieved_at"])),
                            r["flagged"], r["approved"], len(r["text"] or ""), r["content_hash"]])
            out.write_text(buf.getvalue(), encoding="utf-8", newline="")
        elif fmt == "md":
            lines = [f"# Web research: {j['query'] or ', '.join(j['params'].get('urls', []))}", "",
                     f"*Job `{jid}` - parameters: max_pages={j['params']['max_pages']}, max_depth={j['params']['max_depth']}, "
                     f"same_domain={j['params']['same_domain']} - counts: {j['counts']}*", ""]
            for r in rows:
                if r["status"] != "ok":
                    lines.append(f"- ~~{r['url']}~~ - {r['status']}: {(r['meta'] or {}).get('reason', '')}")
                    continue
                lines += [f"## {r['title'] or r['url']}", f"Source: <{r['final_url']}> - retrieved {time.strftime('%Y-%m-%d', time.gmtime(r['retrieved_at']))}"
                          + (f" - **FLAGGED: {r['flagged']}**" if r["flagged"] else ""), "", (r["text"] or "")[:1500], ""]
            out.write_text("\n".join(lines), encoding="utf-8")
        else:
            raise ValueError("format must be json, csv or md")
        return out

    # ---- knowledge hand-off ----------------------------------------------------------------
    def url_fetcher(self, url: str) -> dict:
        """Used by the ingestion pipeline for URL sources: reuse a stored research page when possible."""
        row = self.db.one("SELECT final_url, raw_path FROM research_pages WHERE (url=? OR final_url=?) AND status='ok' ORDER BY retrieved_at DESC LIMIT 1",
                          (normalize_url(url), url))
        if row and row["raw_path"] and Path(row["raw_path"]).is_file():
            return {"path": row["raw_path"], "final_url": row["final_url"]}
        r = self.fetcher.fetch(url, save_dir=self.s.path("web", "adhoc"))
        if r.status != "ok" or not r.raw_path:
            raise RuntimeError(r.reason or "fetch failed")
        return {"path": r.raw_path, "final_url": r.final_url}

    def add_to_knowledge(self, ingestion, jid: str, collection: str, page_ids: list[str] | None = None) -> dict:
        pages = [p for p in self.pages(jid, "ok") if (p["id"] in page_ids if page_ids else p["approved"])]
        if not pages:
            raise ValueError("no approved pages. Review the results and approve pages first.")
        job = ingestion.create_job(f"research-{jid}", collection, [p["final_url"] for p in pages])
        ingestion.run(job["id"])
        return ingestion.get_job(job["id"])
