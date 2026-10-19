"""Streaming parsers. Every parser yields `Segment`s lazily - nothing here loads a whole large file.

Malformed records are reported through the `reject(record_index, reason, sample)` callback instead of
being silently dropped. Tabular parsers also feed a `Profile` (schema + per-column statistics).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

RejectFn = Callable[[int, str, str], None]

TEXT_EXT = {".txt", ".text", ".rst", ".tex", ".readme"}
MD_EXT = {".md", ".markdown"}
HTML_EXT = {".html", ".htm", ".xhtml"}
LOG_EXT = {".log", ".out", ".err"}
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
            ".sh", ".ps1", ".bat", ".sql", ".kt", ".swift", ".lua", ".r", ".scala", ".css", ".vue"}
CONFIG_EXT = {".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml", ".env", ".properties", ".config"}
ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
KINDS = {".pdf": "pdf", ".docx": "docx", ".csv": "csv", ".tsv": "csv", ".json": "json", ".jsonl": "jsonl",
         ".ndjson": "jsonl", ".xlsx": "xlsx", ".xlsm": "xlsx", ".xml": "xml", ".parquet": "parquet"}
for _e in TEXT_EXT | MD_EXT:
    KINDS[_e] = "text"
for _e in HTML_EXT:
    KINDS[_e] = "html"
for _e in LOG_EXT:
    KINDS[_e] = "log"
for _e in CODE_EXT:
    KINDS[_e] = "code"
for _e in CONFIG_EXT:
    KINDS[_e] = "config"
for _e in ARCHIVE_EXT:
    KINDS[_e] = "archive"

TABULAR = {"csv", "jsonl", "json", "xlsx", "parquet", "xml"}


class ParseError(Exception):
    """File cannot be read at all (corrupt, encrypted, wrong format)."""


@dataclass
class Segment:
    text: str
    locator: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class Profile:
    """Streaming column statistics for tabular data (bounded memory)."""

    def __init__(self) -> None:
        self.rows = 0
        self.columns: dict[str, dict[str, Any]] = {}
        self.key_sets: dict[tuple, int] = {}
        self.malformed = 0

    def row(self, rec: dict[str, Any]) -> None:
        self.rows += 1
        sig = tuple(sorted(rec.keys()))
        if sig in self.key_sets or len(self.key_sets) < 50:
            self.key_sets[sig] = self.key_sets.get(sig, 0) + 1
        for k, v in rec.items():
            c = self.columns.setdefault(str(k), {"non_null": 0, "null": 0, "distinct": set(), "num_min": None,
                                                 "num_max": None, "num_sum": 0.0, "num_n": 0, "sample": None})
            if v is None or (isinstance(v, str) and not v.strip()):
                c["null"] += 1
                continue
            c["non_null"] += 1
            if c["sample"] is None:
                c["sample"] = str(v)[:80]
            if len(c["distinct"]) < 1000:
                c["distinct"].add(str(v)[:100])
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            c["num_n"] += 1
            c["num_sum"] += x
            c["num_min"] = x if c["num_min"] is None else min(c["num_min"], x)
            c["num_max"] = x if c["num_max"] is None else max(c["num_max"], x)

    def to_dict(self) -> dict:
        cols = {}
        for k, c in self.columns.items():
            tot = c["non_null"] + c["null"]
            numeric = c["num_n"] > 0 and c["num_n"] >= 0.9 * max(1, c["non_null"])
            cols[k] = {"non_null": c["non_null"], "null": c["null"],
                       "null_pct": round(100 * c["null"] / tot, 1) if tot else 0.0,
                       "distinct_sampled": len(c["distinct"]), "type": "numeric" if numeric else "text",
                       "sample": c["sample"]}
            if numeric:
                cols[k].update(min=c["num_min"], max=c["num_max"], mean=round(c["num_sum"] / c["num_n"], 4))
        variants = sorted(self.key_sets.items(), key=lambda kv: -kv[1])
        return {"rows": self.rows, "malformed_rows": self.malformed, "columns": cols,
                "schema_variants": [{"fields": list(sig), "rows": n} for sig, n in variants[:5]],
                "schema_drift": len(self.key_sets) > 1}


# ---- detection & validation -------------------------------------------------------------------
def detect_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if path.name.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        return "archive"
    if ext in KINDS:
        return KINDS[ext]
    if path.name.lower() in ("dockerfile", "makefile", ".gitignore", ".env"):
        return "config"
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return "unsupported"
    if b"\x00" in head:
        return "unsupported"
    return "text" if head.strip() else "empty"


def validate_file(path: Path, kind: str, max_bytes: int) -> tuple[bool, str]:
    try:
        st = path.stat()
    except OSError as e:
        return False, f"unreadable: {e}"
    if st.st_size == 0:
        return False, "empty file"
    if st.st_size > max_bytes:
        return False, f"file larger than limit ({st.st_size // 2**20} MB > {max_bytes // 2**20} MB)"
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError as e:
        return False, f"unreadable: {e}"
    if kind == "pdf" and not head.startswith(b"%PDF"):
        return False, "corrupt: missing %PDF header"
    if kind in ("docx", "xlsx") and not zipfile.is_zipfile(path):
        return False, f"corrupt: not a valid {kind} (zip) container"
    if kind == "parquet" and head[:4] != b"PAR1":
        return False, "corrupt: missing PAR1 magic"
    if kind == "unsupported":
        return False, "unsupported format (binary data)"
    return True, ""


# ---- helpers -----------------------------------------------------------------------------
def _open_text(path: Path):
    """Open as text: UTF-8 (BOM aware); undecodable bytes become U+FFFD which quality checks count."""
    return open(path, "r", encoding="utf-8-sig", errors="replace", newline=None)


def _flatten(obj: Any, prefix: str = "", out: list[str] | None = None, depth: int = 0) -> list[str]:
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}{k}." if depth < 6 else f"{prefix}{k}", out, depth + 1)
    elif isinstance(obj, list):
        if all(not isinstance(x, (dict, list)) for x in obj):
            out.append(f"{prefix.rstrip('.')}: {', '.join(str(x) for x in obj[:50])}")
        else:
            for i, v in enumerate(obj[:200]):
                _flatten(v, f"{prefix}{i}.", out, depth + 1)
    else:
        out.append(f"{prefix.rstrip('.')}: {obj}")
    return out


def record_text(rec: dict[str, Any]) -> str:
    return " | ".join(f"{k}: {v}" for k, v in rec.items() if v is not None and str(v).strip() != "")


def _group_records(recs: Iterator[tuple[int, str]], header: str, rows_per_segment: int,
                   max_chars: int) -> Iterator[Segment]:
    buf: list[str] = []
    size = 0
    first = last = 0
    for idx, line in recs:
        if not buf:
            first = idx
        buf.append(line)
        size += len(line)
        last = idx
        if len(buf) >= rows_per_segment or size >= max_chars:
            yield Segment((header + "\n" if header else "") + "\n".join(buf), f"rows {first}-{last}", {"rows": len(buf)})
            buf, size = [], 0
    if buf:
        yield Segment((header + "\n" if header else "") + "\n".join(buf), f"rows {first}-{last}", {"rows": len(buf)})


# ---- parsers -----------------------------------------------------------------------------------
def parse_text(path: Path, kind: str, reject: RejectFn, profile: Profile, seg_chars: int = 4000) -> Iterator[Segment]:
    buf: list[str] = []
    size = 0
    start = 1
    n = 0
    bad = 0
    with _open_text(path) as f:
        for n, line in enumerate(f, 1):
            if "�" in line:
                bad += 1
            if not buf:
                start = n
            buf.append(line)
            size += len(line)
            blank_break = not line.strip() and size > seg_chars * 0.6
            if size >= seg_chars or (blank_break and kind in ("text", "code", "config")):
                yield Segment("".join(buf), f"lines {start}-{n}")
                buf, size = [], 0
    if buf:
        yield Segment("".join(buf), f"lines {start}-{n}")
    if bad:
        reject(0, f"encoding problems: {bad} line(s) contained undecodable bytes (replaced with U+FFFD)", "")


def parse_pdf(path: Path, kind: str, reject: RejectFn, profile: Profile) -> Iterator[Segment]:
    import pymupdf as fitz  # PyMuPDF
    try:
        doc = fitz.open(str(path))
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"cannot open PDF: {e}") from e
    try:
        if doc.needs_pass:
            raise ParseError("PDF is password-protected")
        for i, page in enumerate(doc, 1):
            try:
                text = page.get_text("text")
            except Exception as e:  # noqa: BLE001
                reject(i, f"page extraction error: {e}", "")
                continue
            if not text.strip():
                reject(i, "page has no extractable text (scanned image? OCR is not built in)", "")
                continue
            yield Segment(text, f"page {i}", {"page": i})
    finally:
        doc.close()


def parse_docx(path: Path, kind: str, reject: RejectFn, profile: Profile, seg_chars: int = 3000) -> Iterator[Segment]:
    import docx
    try:
        d = docx.Document(str(path))
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"cannot open DOCX: {e}") from e
    buf: list[str] = []
    size = 0
    part = 1
    blocks: list[str] = []
    for p in d.paragraphs:
        if p.text.strip():
            style = (p.style.name or "") if p.style is not None else ""
            blocks.append(("# " + p.text) if style.startswith("Heading") else p.text)
    for t in d.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                blocks.append(" | ".join(cells))
    for b in blocks:
        buf.append(b)
        size += len(b)
        if size >= seg_chars:
            yield Segment("\n".join(buf), f"part {part}")
            buf, size, part = [], 0, part + 1
    if buf:
        yield Segment("\n".join(buf), f"part {part}")


def parse_html(path: Path, kind: str, reject: RejectFn, profile: Profile) -> Iterator[Segment]:
    raw = path.read_bytes()
    text, title = extract_html(raw.decode("utf-8", "replace"))
    if not text.strip():
        raise ParseError("HTML contains no extractable text")
    yield Segment(text, "page", {"title": title})


def extract_html(html: str, url: str | None = None) -> tuple[str, str]:
    """Main-content text + title. trafilatura first, BeautifulSoup as fallback."""
    from bs4 import BeautifulSoup
    title = ""
    try:
        soup = BeautifulSoup(html, "lxml")
        title = soup.title.get_text(strip=True) if soup.title else ""
    except Exception:  # noqa: BLE001
        soup = None
    text = ""
    try:
        import trafilatura
        text = trafilatura.extract(html, url=url, include_tables=True, include_comments=False, favor_recall=True) or ""
    except Exception:  # noqa: BLE001
        text = ""
    if len(text) < 80 and soup is not None:
        for t in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
            t.decompose()
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return text, title


def parse_csv(path: Path, kind: str, reject: RejectFn, profile: Profile, rows_per_segment: int = 25) -> Iterator[Segment]:
    csv.field_size_limit(10_000_000)
    with _open_text(path) as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
        rd = csv.reader(f, dialect)
        try:
            header = next(rd)
        except StopIteration:
            raise ParseError("CSV has no header row") from None
        header = [h.strip() or f"col{i}" for i, h in enumerate(header)]
        if len(set(header)) != len(header):
            reject(0, "duplicate column names in header", ",".join(header)[:200])
        hdr_line = "Columns: " + ", ".join(header)

        def recs() -> Iterator[tuple[int, str]]:
            try:
                for i, row in enumerate(rd, 1):
                    if not row or all(not c.strip() for c in row):
                        reject(i, "blank row", "")
                        profile.malformed += 1
                        continue
                    if len(row) != len(header):
                        reject(i, f"malformed row: {len(row)} fields, expected {len(header)}", ",".join(row)[:200])
                        profile.malformed += 1
                        continue
                    rec = dict(zip(header, row))
                    profile.row(rec)
                    yield i, record_text(rec)
            except csv.Error as e:
                reject(0, f"CSV parse error: {e}", "")

        yield from _group_records(recs(), hdr_line, rows_per_segment, 6000)


def parse_jsonl(path: Path, kind: str, reject: RejectFn, profile: Profile, rows_per_segment: int = 20) -> Iterator[Segment]:
    def recs() -> Iterator[tuple[int, str]]:
        with _open_text(path) as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError as e:
                    reject(i, f"malformed JSON: {e}", line[:200])
                    profile.malformed += 1
                    continue
                if isinstance(obj, dict):
                    profile.row(obj)
                    yield i, "; ".join(_flatten(obj))
                elif isinstance(obj, str):
                    yield i, obj
                else:
                    yield i, json.dumps(obj, ensure_ascii=False)
    yield from _group_records(recs(), "", rows_per_segment, 6000)


def parse_json(path: Path, kind: str, reject: RejectFn, profile: Profile, rows_per_segment: int = 20) -> Iterator[Segment]:
    import ijson
    with open(path, "rb") as f:
        first = f.read(1)
        while first and first in b" \t\r\n\xef\xbb\xbf":
            first = f.read(1)
    if first == b"[":
        def recs() -> Iterator[tuple[int, str]]:
            with open(path, "rb") as f:
                try:
                    for i, obj in enumerate(ijson.items(f, "item", use_float=True), 1):
                        if isinstance(obj, dict):
                            profile.row(obj)
                            yield i, "; ".join(_flatten(obj))
                        else:
                            yield i, str(obj)
                except ijson.JSONError as e:
                    reject(0, f"malformed JSON (stopped reading): {e}", "")
        yield from _group_records(recs(), "", rows_per_segment, 6000)
    elif first == b"{":
        if path.stat().st_size > 50 * 2**20:
            raise ParseError("large top-level JSON object cannot be streamed; convert to JSONL")
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        except ValueError as e:
            raise ParseError(f"malformed JSON: {e}") from e
        lines = _flatten(obj)
        for i in range(0, len(lines), 60):
            yield Segment("\n".join(lines[i:i + 60]), f"fields {i + 1}-{i + 60}")
    else:
        raise ParseError("JSON file must start with [ or {")


def parse_xlsx(path: Path, kind: str, reject: RejectFn, profile: Profile, rows_per_segment: int = 25) -> Iterator[Segment]:
    import openpyxl
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"cannot open workbook: {e}") from e
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            header = next(rows, None)
            if not header or not any(h is not None for h in header):
                continue
            header = [str(h).strip() if h is not None else f"col{i}" for i, h in enumerate(header)]

            def recs() -> Iterator[tuple[int, str]]:
                for i, row in enumerate(rows, 1):
                    if row is None or all(c is None or str(c).strip() == "" for c in row):
                        continue
                    rec = {h: c for h, c in zip(header, row)}
                    profile.row(rec)
                    yield i, record_text(rec)
            for seg in _group_records(recs(), f"Sheet {ws.title}. Columns: " + ", ".join(header), rows_per_segment, 6000):
                seg.locator = f"{ws.title} {seg.locator}"
                yield seg
    finally:
        wb.close()


def parse_xml(path: Path, kind: str, reject: RejectFn, profile: Profile, rows_per_segment: int = 20) -> Iterator[Segment]:
    import xml.etree.ElementTree as ET

    def recs() -> Iterator[tuple[int, str]]:
        depth = 0
        n = 0
        stack: list[Any] = []
        try:
            for ev, el in ET.iterparse(str(path), events=("start", "end")):
                if ev == "start":
                    depth += 1
                    stack.append(el)
                    continue
                depth -= 1
                stack.pop()
                if depth == 1:  # a direct child of the root = one record
                    parts = []
                    for sub in el.iter():
                        t = (sub.text or "").strip()
                        if t:
                            parts.append(f"{sub.tag}: {t}")
                        for ak, av in sub.attrib.items():
                            parts.append(f"{sub.tag}@{ak}: {av}")
                    n += 1
                    if parts:
                        yield n, "; ".join(parts)
                    el.clear()
        except ET.ParseError as e:
            reject(0, f"malformed XML (stopped reading): {e}", "")
    yield from _group_records(recs(), "", rows_per_segment, 6000)


def parse_parquet(path: Path, kind: str, reject: RejectFn, profile: Profile, rows_per_segment: int = 25) -> Iterator[Segment]:
    import pyarrow.parquet as pq
    try:
        pf = pq.ParquetFile(str(path))
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"cannot open Parquet: {e}") from e
    base = 0

    def recs() -> Iterator[tuple[int, str]]:
        nonlocal base
        for batch in pf.iter_batches(batch_size=1000):
            cols = batch.to_pydict()
            names = list(cols)
            for j in range(batch.num_rows):
                rec = {n: cols[n][j] for n in names}
                profile.row(rec)
                base += 1
                yield base, record_text(rec)
    yield from _group_records(recs(), "", rows_per_segment, 6000)


PARSERS: dict[str, Callable[..., Iterator[Segment]]] = {
    "text": parse_text, "log": parse_text, "code": parse_text, "config": parse_text,
    "pdf": parse_pdf, "docx": parse_docx, "html": parse_html, "csv": parse_csv, "jsonl": parse_jsonl,
    "json": parse_json, "xlsx": parse_xlsx, "xml": parse_xml, "parquet": parse_parquet,
}


def iter_segments(path: Path, kind: str, reject: RejectFn, profile: Profile) -> Iterator[Segment]:
    fn = PARSERS.get(kind)
    if fn is None:
        raise ParseError(f"no parser for kind '{kind}'")
    return fn(path, kind, reject, profile)


# ---- archives ---------------------------------------------------------------------------------
def safe_extract(archive: Path, dest: Path, max_members: int, max_ratio: int, max_total: int) -> list[Path]:
    """Extract supported members with zip-slip, zip-bomb and symlink protection."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    total = 0
    dest_r = dest.resolve()

    def target(name: str) -> Path | None:
        p = (dest / name).resolve()
        if dest_r != p and dest_r not in p.parents:
            return None  # path traversal
        return p

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            if len(infos) > max_members:
                raise ParseError(f"archive has {len(infos)} members (limit {max_members})")
            for zi in infos:
                if zi.is_dir():
                    continue
                total += zi.file_size
                if total > max_total:
                    raise ParseError("archive expands beyond the size limit (possible zip bomb)")
                if zi.compress_size and zi.file_size / max(1, zi.compress_size) > max_ratio and zi.file_size > 10 * 2**20:
                    raise ParseError(f"suspicious compression ratio on '{zi.filename}' (possible zip bomb)")
                t = target(zi.filename)
                if t is None:
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                with z.open(zi) as src, open(t, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                out.append(t)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            members = tf.getmembers()
            if len(members) > max_members:
                raise ParseError(f"archive has {len(members)} members (limit {max_members})")
            for m in members:
                if not m.isfile():
                    continue  # skips symlinks, devices, dirs
                total += m.size
                if total > max_total:
                    raise ParseError("archive expands beyond the size limit")
                t = target(m.name)
                if t is None:
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src, open(t, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                out.append(t)
    elif archive.suffix.lower() == ".gz":
        import gzip
        t = dest / archive.stem
        with gzip.open(archive, "rb") as src, open(t, "wb") as dst:
            n = 0
            while chunk := src.read(1 << 20):
                n += len(chunk)
                if n > max_total:
                    raise ParseError("gzip expands beyond the size limit")
                dst.write(chunk)
        out.append(t)
    else:
        raise ParseError("unrecognised or corrupt archive")
    return out
