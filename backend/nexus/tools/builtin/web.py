"""Web tools. Everything these return is third-party content -> flagged untrusted."""
from __future__ import annotations

import time

from ...permissions.policy import NET_READ, SCRATCH
from ...web_research.research import SearchError, web_search as _search
from ..registry import Tool, ToolContext, ToolRegistry, fail, ok, truncate


def web_search(ctx: ToolContext, a: dict) -> dict:
    try:
        res = _search(ctx.app.settings, a["query"], int(a.get("max_results", 5)), getattr(ctx.app, "search_transport", None))
    except SearchError as e:
        return fail(str(e))
    return ok(query=a["query"], retrieved_at=time.time(), results=res)


def web_fetch(ctx: ToolContext, a: dict) -> dict:
    r = ctx.app.research.fetcher.fetch(a["url"], js=bool(a.get("js")), save_dir=ctx.app.settings.path("web", "adhoc"))
    if r.status != "ok":
        return fail(f"{r.status}: {r.reason}", url=a["url"], http_status=r.http_status)
    text, trunc = truncate(r.text, int(a.get("max_chars", 6000)))
    return ok(url=a["url"], final_url=r.final_url, title=r.title, retrieved_at=r.retrieved_at, text=text, truncated=trunc,
              headings=r.headings[:20], link_count=len(r.links), meta=r.meta)


def web_scrape(ctx: ToolContext, a: dict) -> dict:
    r = ctx.app.research.fetcher.fetch(a["url"], js=bool(a.get("js")), save_dir=ctx.app.settings.path("web", "adhoc"))
    if r.status != "ok":
        return fail(f"{r.status}: {r.reason}", url=a["url"])
    what = a.get("extract", ["headings", "links", "tables", "metadata"])
    out: dict = {"final_url": r.final_url, "title": r.title, "retrieved_at": r.retrieved_at}
    if "headings" in what:
        out["headings"] = r.headings
    if "links" in what:
        out["links"] = r.links[:int(a.get("max_links", 50))]
    if "tables" in what:
        out["tables"] = r.tables[:5]
    if "metadata" in what:
        out["metadata"] = r.meta
    return ok(**out)


def web_research(ctx: ToolContext, a: dict) -> dict:
    rm = ctx.app.research
    job = rm.create_job(a.get("query", ""), a.get("urls"), {"max_pages": min(int(a.get("max_pages", 5)), 15), "max_depth": min(int(a.get("max_depth", 0)), 2),
                                                            "same_domain": True})
    j = rm.run(job["id"])
    pages = rm.pages(job["id"])
    if j["status"] == "failed":
        return fail(j["error"], job=job["id"])
    return ok(job=job["id"], counts=j["counts"], pages=[{"url": p["final_url"], "status": p["status"], "title": p["title"], "flagged": p["flagged"],
                                                       "preview": (p.get("preview") or "")[:300]} for p in pages][:20],
              note="Pages are stored as UNAPPROVED. A human reviews them before anything enters the knowledge base.")


def register(reg: ToolRegistry) -> None:
    S = lambda **p: {"type": "object", "properties": p, "additionalProperties": False}  # noqa: E731
    reg.register(Tool("web_search", "Search the public web via the configured search provider. Returns titles, URLs and snippets.",
                      {**S(query={"type": "string"}, max_results={"type": "integer"}), "required": ["query"]}, web_search, effects={NET_READ},
                      untrusted_output=True, timeout=40))
    reg.register(Tool("web_fetch", "Fetch one public web page (respects robots.txt and domain rules) and return its main text.",
                      {**S(url={"type": "string"}, max_chars={"type": "integer"}, js={"type": "boolean"}), "required": ["url"]}, web_fetch,
                      effects={NET_READ}, untrusted_output=True, timeout=60, targets_fn=None))
    reg.register(Tool("web_scrape", "Extract headings, links, tables and metadata from a public page.",
                      {**S(url={"type": "string"}, extract={"type": "array", "items": {"type": "string"}}, max_links={"type": "integer"}, js={"type": "boolean"}),
                       "required": ["url"]}, web_scrape, effects={NET_READ}, untrusted_output=True, timeout=60))
    reg.register(Tool("web_research", "Run a bounded research crawl (search and/or URLs) and store the pages for human review.",
                      S(query={"type": "string"}, urls={"type": "array", "items": {"type": "string"}}, max_pages={"type": "integer"}, max_depth={"type": "integer"}),
                      web_research, effects={NET_READ, SCRATCH}, risk="medium", untrusted_output=True, timeout=600))
