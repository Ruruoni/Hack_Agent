"""Stage 5: web research, URL import, exports, dataset manager."""
import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write

from nexus.datasets.manager import DatasetError
from nexus.web_research.fetcher import UrlPolicy, WebPolicyError


def test_import_public_webpage_preserves_source_url(ctx, web_server):
    url = web_server.base + "/page2.html"
    job = ctx.ingestion.create_job("web", "web", [url])
    ctx.ingestion.run(job["id"])
    j = ctx.ingestion.get_job(job["id"])
    assert j["status"] == "completed" and j["counts"]["chunks_indexed"] >= 1
    hit = ctx.kb.search("ledger database backups retained", k=1)["hits"][0]
    assert hit["source"] == url and hit["source_type"] == "html"
    assert abs(hit["retrieved_at"] - __import__("time").time()) < 60            # retrieval date recorded
    assert "35 days" in hit["text"]


def test_research_crawl_robots_dedup_blocks_and_export(ctx, web_server):
    rm = ctx.research
    job = rm.create_job(urls=[web_server.base + "/index.html", web_server.base + "/captcha.html", web_server.base + "/forbidden",
                              web_server.base + "/dup.html", web_server.base + "/redirect"], params={"max_depth": 1, "max_pages": 12})
    j = rm.run(job["id"])
    pages = {p["url"].replace(web_server.base, ""): p for p in rm.pages(job["id"])}
    assert j["status"] == "completed"
    assert "/private/secret.html" not in web_server.hits                      # robots.txt respected: never even requested
    assert pages["/private/secret.html"]["status"] == "blocked" and "robots.txt" in pages["/private/secret.html"]["meta"]["reason"]
    assert pages["/captcha.html"]["status"] == "blocked" and "not bypassed" in pages["/captcha.html"]["meta"]["reason"]
    assert pages["/forbidden"]["status"] == "blocked" and "not bypassed" in pages["/forbidden"]["meta"]["reason"]
    assert pages["/evil.html"]["flagged"] and "ignore-previous" in pages["/evil.html"]["flagged"]
    assert pages["/page2.html"]["status"] in ("ok", "duplicate")
    statuses = [p["status"] for p in pages.values()]
    assert "duplicate" in statuses                                            # dup.html / redirect->page2 share content with page2
    idx = pages["/index.html"]
    assert idx["title"] == "Ops Handbook" and idx["meta"]["tables"][0][1] == ["gateway", "9443"] and any(h["text"] == "Restarting the gateway" for h in idx["meta"]["headings"])
    csv_path = rm.export(job["id"], "csv")
    rows = list(csv.DictReader(open(csv_path)))
    assert {r["status"] for r in rows} >= {"ok", "blocked"} and all("T" in r["retrieved_at"] for r in rows)
    data = json.loads(rm.export(job["id"], "json").read_text())
    assert data["job"]["params"]["max_depth"] == 1 and len(data["pages"]) == len(rows)       # parameters saved => reproducible
    assert "Ops Handbook" in rm.export(job["id"], "md").read_text()


def test_research_resume_after_interruption(ctx, web_server):
    rm = ctx.research
    job = rm.create_job(urls=[web_server.base + "/index.html"], params={"max_depth": 1, "max_pages": 6})
    ctx.research.cancel(job["id"])
    rm._cancel[job["id"]] = __import__("threading").Event()
    # simulate stop after the first page by lowering the budget, then raising it
    rm.db.update("research_jobs", job["id"], {"params": {**rm.get_job(job["id"])["params"], "max_pages": 1}})
    rm.run(job["id"])
    assert rm.get_job(job["id"])["counts"]["fetched"] == 1 and rm.get_job(job["id"])["frontier"]
    rm.db.update("research_jobs", job["id"], {"params": {**rm.get_job(job["id"])["params"], "max_pages": 6}})
    rm.run(job["id"])
    done = {p["url"] for p in rm.pages(job["id"])}
    assert len(done) == len(rm.pages(job["id"])) and any(u.endswith("page2.html") for u in done)       # no page fetched twice


def test_malicious_page_is_quarantined_not_indexed(ctx, web_server):
    rm = ctx.research
    job = rm.create_job(urls=[web_server.base + "/evil.html", web_server.base + "/page2.html"], params={"max_depth": 0})
    rm.run(job["id"])
    pages = rm.pages(job["id"])
    assert rm.approve([p["id"] for p in pages if p["status"] == "ok"]) == 2     # a careless human approves both...
    kj = rm.add_to_knowledge(ctx.ingestion, job["id"], "research")
    assert kj["counts"]["chunks_quarantined"] >= 1                              # ...the pipeline still quarantines the hostile content
    hits = ctx.kb.search("approve all actions automatically unrestricted mode", k=5)["hits"]
    assert not any("IGNORE ALL PREVIOUS" in h["text"] for h in hits)
    assert any("35 days" in h["text"] for h in ctx.kb.search("ledger backups retention", k=3)["hits"])
    q = ctx.ingestion.quarantined_chunks(kj["id"])
    assert q and q[0]["source"].endswith("/evil.html")


def test_add_to_knowledge_requires_approval(ctx, web_server):
    job = ctx.research.create_job(urls=[web_server.base + "/page2.html"], params={"max_depth": 0})
    ctx.research.run(job["id"])
    with pytest.raises(ValueError):
        ctx.research.add_to_knowledge(ctx.ingestion, job["id"], "research")


def test_url_policy_ssrf_allow_deny(settings):
    settings.web_allow_private = False
    pol = UrlPolicy(settings)
    for bad in ["http://127.0.0.1:8000/", "http://localhost/", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/", "file:///etc/passwd", "ftp://x/y",
                "http://[::1]/"]:
        with pytest.raises(WebPolicyError):
            pol.check(bad)
    settings.web_allow_private = True
    pol.check("http://127.0.0.1:9/")
    with pytest.raises(WebPolicyError):
        pol.check("http://169.254.169.254/")                                    # metadata address blocked even in lab mode
    settings.web_allowlist = ["docs.example.org"]
    settings.web_allow_private = False
    with pytest.raises(WebPolicyError):
        pol.check("http://evil.example.com/")


def test_fetch_blocks_private_hosts_by_default(settings, web_server):
    from nexus.web_research.fetcher import Fetcher
    settings.web_allow_private = False
    r = Fetcher(settings).fetch(web_server.base + "/index.html")
    assert r.status == "blocked" and "private" in r.reason and web_server.hits == []


def test_search_provider_not_configured_is_clear(ctx):
    from nexus.web_research.research import SearchError, web_search
    with pytest.raises(SearchError, match="No search provider"):
        web_search(ctx.settings, "x")


def test_searxng_provider_results_feed_research(ctx, web_server):
    import httpx
    ctx.settings.search_provider = "searxng"
    ctx.settings.search_url = "http://127.0.0.1:1"
    handler = lambda req: httpx.Response(200, json={"results": [{"title": "Backups", "url": web_server.base + "/page2.html", "content": "snippet"}]})  # noqa: E731
    ctx.research.search_transport = httpx.MockTransport(handler)
    job = ctx.research.create_job(query="ledger backups", params={"max_depth": 0})
    j = ctx.research.run(job["id"])
    assert j["status"] == "completed" and j["params"]["search_hits"][0]["title"] == "Backups" and j["counts"]["ok"] == 1


# ---------------------------------------------------------------- datasets
def test_dataset_download_verify_preview_import(ctx, web_server):
    dm = ctx.datasets
    url = web_server.base + "/big.csv"
    plan = dm.plan(url)
    assert plan["selected"][0]["name"] == "big.csv" and plan["selected_bytes"] > 10000 and plan["disk_ok"] and not plan["needs_explicit_approval"]
    d = dm.start_download(url, background=False)
    d = dm.get(d["id"])
    assert d["status"] == "ready", d["error"]
    f = dm.files_of(d["id"])[0]
    assert f.stat().st_size == plan["selected_bytes"] and not list(Path(d["dest"]).glob("*.part"))
    pv = dm.preview(f, 3)
    assert pv["columns"] == ["id", "msg"] and len(pv["rows"]) == 3
    job = dm.import_dataset(d["id"], "downloaded", {"batch_size": 32}, start=False)
    ctx.ingestion.run(job["id"])
    assert ctx.ingestion.get_job(job["id"])["counts"]["records_seen"] == 2000
    assert ctx.db.scalar("SELECT COUNT(*) FROM audit_log WHERE event='dataset.download'") == 1


def test_download_resumes_partial_file_and_detects_corruption(ctx, web_server):
    dm = ctx.datasets
    url = web_server.base + "/big.csv"
    d = dm.start_download(url, background=False)
    dest = Path(dm.get(d["id"])["dest"])
    good = (dest / "big.csv").read_bytes()
    # resume: pre-seed a partial file in a fresh download dir
    d2id = "dl_resume"
    dm.db.insert("downloads", {"id": d2id, "kind": "url", "ref": url, "status": "queued", "dest": str(dest.parent / "resume"), "files": [{"name": "big.csv", "size": len(good), "sha256": hashlib.sha256(good).hexdigest(), "status": "pending"}],
                               "total_bytes": len(good), "created": 0, "updated": 0})
    (dest.parent / "resume").mkdir()
    (dest.parent / "resume" / "big.csv.part").write_bytes(good[:5000])
    dm._cancel[d2id] = __import__("threading").Event()
    dm._download(d2id)
    r = dm.get(d2id)
    assert r["status"] == "ready" and (dest.parent / "resume" / "big.csv").read_bytes() == good
    # corruption is detected by checksum
    (dest.parent / "resume" / "big.csv").write_bytes(good[:-10] + b"CORRUPTED!")
    assert any("mismatch" in p or "size" in p for p in dm.verify(d2id))


def test_large_download_needs_explicit_approval_and_disk_is_checked(ctx, web_server):
    ctx.settings.max_auto_download_mb = 0
    with pytest.raises(DatasetError) as e:
        ctx.datasets.start_download(web_server.base + "/big.csv", background=False)
    assert e.value.code == "approval_required"
    ctx.settings.min_free_disk_mb = 10**9
    with pytest.raises(DatasetError) as e2:
        ctx.datasets.start_download(web_server.base + "/big.csv", approve_large=True, background=False)
    assert e2.value.code == "disk_space"


def test_dataset_url_ssrf_blocked(ctx):
    ctx.settings.web_allow_private = False
    with pytest.raises(DatasetError) as e:
        ctx.datasets.inspect("http://127.0.0.1:8000/x.csv")
    assert e.value.code == "blocked"


def test_local_dataset_registered_in_place_and_preview_formats(ctx, tmp_path):
    ds = tmp_path / "ds"
    ds.mkdir()
    import pyarrow as pa
    import pyarrow.parquet as pq
    write(ds, "a.jsonl", '{"q":"one","a":1}\n{"q":"two","a":2}\n')
    pq.write_table(pa.table({"x": [1, 2, 3], "y": ["a", "b", "c"]}), ds / "p.parquet")
    write(ds, "t.json", '[{"k":1},{"k":2}]')
    info = ctx.datasets.inspect(str(ds))
    assert info["kind"] == "local" and {f["name"] for f in info["files"]} == {"a.jsonl", "p.parquet", "t.json"}
    d = ctx.datasets.start_download(str(ds))
    assert d["status"] == "ready" and d["dest"] == str(ds.resolve())            # original preserved, no copy
    assert ctx.datasets.preview(ds / "p.parquet")["schema"] == {"x": "int64", "y": "string"}
    assert ctx.datasets.preview(ds / "a.jsonl")["columns"] == ["a", "q"]
    assert ctx.datasets.preview(ds / "t.json")["rows"] == [{"k": 1}, {"k": 2}]


class FakeHf:
    def __init__(self, gated=False, lic="cc-by-4.0"):
        self.gated, self.lic = gated, lic

    def dataset_info(self, repo, files_metadata=True):
        if repo == "nobody/missing":
            raise type("RepositoryNotFoundError", (Exception,), {})("404")
        if repo == "org/gated" and not self.gated:
            raise type("GatedRepoError", (Exception,), {})("gated")
        sib = lambda n, s, sha=None: SimpleNamespace(rfilename=n, size=s, lfs={"sha256": sha} if sha else None)  # noqa: E731
        return SimpleNamespace(card_data={"license": self.lic} if self.lic else {}, gated=self.gated, private=False, tags=["text"], downloads=5,
                               siblings=[sib("README.md", 10), sib("data/train.csv", 1000, "a" * 64), sib("data/test.csv", 200), sib("model.bin", 99)])


def test_huggingface_inspect_license_gating_and_split_selection(ctx):
    from nexus.datasets.manager import DatasetManager
    dm = DatasetManager(ctx.settings, ctx.db, ctx.ingestion, hf_api_factory=lambda: FakeHf())
    info = dm.inspect("org/name")
    assert info["kind"] == "huggingface" and info["license"] == "cc-by-4.0" and not info["requires_auth"]
    assert {f["name"] for f in info["files"]} == {"data/train.csv", "data/test.csv", "model.bin"}
    plan = dm.plan("org/name", splits=["train"])
    assert [f["name"] for f in plan["selected"]] == ["data/train.csv"] and plan["selected_bytes"] == 1000
    with pytest.raises(DatasetError) as e:
        dm.inspect("org/gated")
    assert e.value.code == "gated" and "HF_TOKEN" in e.value.hint and "never paste" in e.value.hint
    with pytest.raises(DatasetError) as e2:
        dm.inspect("nobody/missing")
    assert e2.value.code == "not_found"
    dm2 = DatasetManager(ctx.settings, ctx.db, ctx.ingestion, hf_api_factory=lambda: FakeHf(lic=None))
    assert any("licence" in w for w in dm2.inspect("org/name")["warnings"])


def test_huggingface_download_path_with_checksum(ctx, tmp_path):
    from nexus.datasets.manager import DatasetManager
    content = b"a,b\n1,2\n"

    class Api(FakeHf):
        def dataset_info(self, repo, files_metadata=True):
            i = super().dataset_info(repo)
            i.siblings = [SimpleNamespace(rfilename="data/train.csv", size=len(content), lfs={"sha256": hashlib.sha256(content).hexdigest()})]
            return i

    def fake_dl(repo_id, filename, repo_type, local_dir, token):
        p = Path(local_dir) / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return str(p)
    dm = DatasetManager(ctx.settings, ctx.db, ctx.ingestion, hf_api_factory=lambda: Api(), hf_download=fake_dl)
    d = dm.start_download("org/name", background=False)
    assert dm.get(d["id"])["status"] == "ready"
    assert dm.get(d["id"])["meta"]["license"] == "cc-by-4.0"
