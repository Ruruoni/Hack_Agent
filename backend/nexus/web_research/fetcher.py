"""Polite, policy-checked web fetching. Never bypasses logins, paywalls, CAPTCHAs or anti-bot measures: such
responses are recorded as 'blocked' with the reason. All retrieved content is treated as untrusted data."""
from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import re
import socket
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from ..config import Settings
from ..ingestion.parsers import extract_html

ALWAYS_BLOCKED_IPS = {ipaddress.ip_address("169.254.169.254")}   # cloud metadata endpoint
CAPTCHA_MARKERS = ("g-recaptcha", "h-captcha", "cf-challenge", "cf-turnstile", "attention required! | cloudflare",
                   "verify you are human", "are you a robot", "unusual traffic from your computer", "checking your browser before accessing")
PAYWALL_MARKERS = ("subscribe to continue", "subscribe to read", "create a free account to continue", "this content is for subscribers",
                   "log in to continue reading")


class WebPolicyError(Exception):
    pass


def normalize_url(url: str) -> str:
    u, _ = urldefrag(url.strip())
    p = urlparse(u)
    path = p.path or "/"
    q = "&".join(sorted(x for x in p.query.split("&") if x and not x.lower().startswith(("utm_", "fbclid", "gclid"))))
    return f"{p.scheme.lower()}://{p.netloc.lower()}{path}" + (f"?{q}" if q else "")


class UrlPolicy:
    def __init__(self, s: Settings):
        self.s = s

    def _match(self, host: str, patterns: list[str]) -> bool:
        host = host.lower()
        return any(host == p.lower() or fnmatch.fnmatch(host, p.lower()) or host.endswith("." + p.lower().lstrip("*.")) for p in patterns)

    def check(self, url: str, explicit_hosts: list[str] | None = None) -> str:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise WebPolicyError(f"only http/https URLs are allowed ({url[:80]})")
        host = p.hostname
        explicit = explicit_hosts and host in explicit_hosts
        if not explicit:
            if self.s.web_denylist and self._match(host, self.s.web_denylist) and not (self.s.web_allow_private and host in ("localhost", "127.0.0.1")):
                raise WebPolicyError(f"domain '{host}' is on the deny-list")
            if self.s.web_allowlist and not self._match(host, self.s.web_allowlist):
                raise WebPolicyError(f"domain '{host}' is not on the allow-list")
        try:
            infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise WebPolicyError(f"cannot resolve '{host}': {e}") from e
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip in ALWAYS_BLOCKED_IPS:
                raise WebPolicyError("access to the cloud metadata address is never allowed")
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified) \
                    and not self.s.web_allow_private:
                raise WebPolicyError(f"'{host}' resolves to a private/loopback address ({ip}); blocked to protect local services "
                                     "(a human can enable 'allow private hosts' for lab use)")
        return url


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: str                       # ok | blocked | error
    http_status: int = 0
    content_type: str = ""
    title: str = ""
    text: str = ""
    html: str = ""
    headings: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    reason: str = ""
    retrieved_at: float = 0.0
    content_hash: str = ""
    raw_path: str = ""


class Fetcher:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.s = settings
        self.policy = UrlPolicy(settings)
        self._transport = transport
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _client(self) -> httpx.Client:
        return httpx.Client(headers={"User-Agent": self.s.web_user_agent, "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf;q=0.8,*/*;q=0.5"},
                            timeout=20, follow_redirects=False, transport=self._transport)

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self._last.get(host, 0) + self.s.web_rate_limit_s - time.time()
            self._last[host] = max(time.time(), self._last.get(host, 0) + self.s.web_rate_limit_s)
        if wait > 0:
            time.sleep(wait)

    def robots_allows(self, client: httpx.Client, url: str) -> bool:
        if not self.s.web_respect_robots:
            return True
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = client.get(origin + "/robots.txt")
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif r.status_code in (401, 403):
                    rp.disallow_all = True
                else:
                    rp.allow_all = True
                self._robots[origin] = rp
            except httpx.HTTPError:
                self._robots[origin] = None       # robots unreachable: treat as allowed (standard practice)
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self.s.web_user_agent, url)

    def fetch(self, url: str, js: bool = False, save_dir: Path | None = None) -> FetchResult:
        res = FetchResult(url=url, final_url=url, status="error", retrieved_at=time.time())
        try:
            self.policy.check(url)
        except WebPolicyError as e:
            res.status, res.reason = "blocked", str(e)
            return res
        if js:
            return self._fetch_js(url, res, save_dir)
        try:
            with self._client() as c:
                cur = url
                for _hop in range(6):
                    self.policy.check(cur)
                    if not self.robots_allows(c, cur):
                        res.status, res.reason, res.final_url = "blocked", "robots.txt disallows this URL", cur
                        return res
                    self._throttle(urlparse(cur).netloc)
                    with c.stream("GET", cur) as r:
                        if r.is_redirect and r.headers.get("location"):
                            cur = urljoin(cur, r.headers["location"])
                            continue
                        res.final_url, res.http_status = cur, r.status_code
                        res.content_type = r.headers.get("content-type", "").split(";")[0].strip().lower()
                        if r.status_code in (401, 403, 407, 429, 451):
                            res.status = "blocked"
                            res.reason = {401: "login required", 403: "access forbidden", 429: "rate limited", 451: "unavailable for legal reasons"}.get(
                                r.status_code, f"HTTP {r.status_code}") + " (not bypassed)"
                            return res
                        if r.status_code >= 400:
                            res.reason = f"HTTP {r.status_code}"
                            return res
                        ok_types = ("text/", "application/xhtml", "application/xml", "application/json", "application/pdf")
                        if res.content_type and not res.content_type.startswith(ok_types):
                            res.reason = f"unsupported content type {res.content_type}"
                            return res
                        buf = bytearray()
                        for chunk in r.iter_bytes():
                            buf += chunk
                            if len(buf) > self.s.web_max_bytes:
                                res.meta["truncated_at_bytes"] = self.s.web_max_bytes
                                break
                        break
                else:
                    res.reason = "too many redirects"
                    return res
        except WebPolicyError as e:
            res.status, res.reason = "blocked", str(e)
            return res
        except httpx.HTTPError as e:
            res.reason = f"network error: {e.__class__.__name__}: {e}"
            return res
        raw = bytes(buf)
        res.content_hash = hashlib.sha256(raw).hexdigest()
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            ext = ".pdf" if res.content_type == "application/pdf" else ".html" if "html" in res.content_type else ".txt"
            fp = save_dir / (hashlib.sha1(res.final_url.encode()).hexdigest()[:16] + ext)
            fp.write_bytes(raw)
            res.raw_path = str(fp)
        if res.content_type == "application/pdf":
            return self._pdf(res, raw)
        text = raw.decode(r.encoding or "utf-8", "replace") if raw else ""
        low = text[:200000].lower()
        if any(m in low for m in CAPTCHA_MARKERS):
            res.status, res.reason = "blocked", "CAPTCHA / anti-bot challenge detected (not bypassed)"
            return res
        if "html" in res.content_type or "<html" in low[:2000]:
            self._extract(res, text)
        else:
            res.text = text
            res.title = res.final_url
        if any(m in low for m in PAYWALL_MARKERS) and len(res.text) < 1500:
            res.meta["possible_paywall"] = True
        res.status = "ok" if res.text.strip() else "error"
        if res.status == "error":
            res.reason = "no extractable text"
        res.retrieved_at = time.time()
        return res

    def _pdf(self, res: FetchResult, raw: bytes) -> FetchResult:
        import pymupdf
        try:
            doc = pymupdf.open(stream=raw, filetype="pdf")
            res.text = "\n\n".join(p.get_text() for p in doc)
            res.title = (doc.metadata or {}).get("title") or res.final_url
            res.status = "ok" if res.text.strip() else "error"
            res.reason = "" if res.text.strip() else "PDF has no extractable text"
        except Exception as e:  # noqa: BLE001
            res.reason = f"cannot parse PDF: {e}"
        return res

    def _fetch_js(self, url: str, res: FetchResult, save_dir: Path | None) -> FetchResult:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            res.reason = "Playwright is not installed (pip install playwright && playwright install chromium)"
            return res
        try:
            with sync_playwright() as pw:
                b = pw.chromium.launch()
                pg = b.new_page(user_agent=self.s.web_user_agent)
                pg.goto(url, timeout=30000)
                html = pg.content()
                res.final_url = pg.url
                b.close()
            self.policy.check(res.final_url)
        except WebPolicyError as e:
            res.status, res.reason = "blocked", str(e)
            return res
        except Exception as e:  # noqa: BLE001
            res.reason = f"browser error: {e}"
            return res
        res.http_status, res.content_type = 200, "text/html"
        res.content_hash = hashlib.sha256(html.encode()).hexdigest()
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            fp = save_dir / (hashlib.sha1(res.final_url.encode()).hexdigest()[:16] + ".html")
            fp.write_text(html, encoding="utf-8")
            res.raw_path = str(fp)
        low = html.lower()
        if any(m in low for m in CAPTCHA_MARKERS):
            res.status, res.reason = "blocked", "CAPTCHA / anti-bot challenge detected (not bypassed)"
            return res
        self._extract(res, html)
        res.status = "ok" if res.text.strip() else "error"
        return res

    def _extract(self, res: FetchResult, html: str) -> None:
        from bs4 import BeautifulSoup
        text, title = extract_html(html, res.final_url)
        res.text, res.title, res.html = text, title or res.final_url, html if len(html) < 400_000 else ""
        soup = BeautifulSoup(html, "lxml")
        res.headings = [{"level": int(h.name[1]), "text": h.get_text(" ", strip=True)[:200]} for h in soup.find_all(re.compile("^h[1-3]$"))][:60]
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("#", "mailto:", "javascript:", "tel:")):
                continue
            u = normalize_url(urljoin(res.final_url, href))
            if u not in seen and urlparse(u).scheme in ("http", "https"):
                seen.add(u)
                res.links.append({"url": u, "text": a.get_text(" ", strip=True)[:120]})
            if len(res.links) >= 300:
                break
        for t in soup.find_all("table")[:10]:
            rows = [[c.get_text(" ", strip=True)[:200] for c in tr.find_all(["th", "td"])] for tr in t.find_all("tr")[:100]]
            if rows and any(any(c for c in r) for r in rows):
                res.tables.append(rows)
        meta = {}
        for m in soup.find_all("meta"):
            k = m.get("name") or m.get("property")
            if k and m.get("content") and k.lower() in ("description", "author", "og:title", "og:description", "og:site_name",
                                                        "article:published_time", "keywords", "robots"):
                meta[k.lower()] = m["content"][:300]
        canon = soup.find("link", rel="canonical")
        if canon and canon.get("href"):
            meta["canonical"] = canon["href"]
        res.meta.update(meta)
