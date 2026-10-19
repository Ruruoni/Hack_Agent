"""Data analysis and technical analysis modules (code, config, logs, packet captures, datasets)."""
from __future__ import annotations

import ast
import collections
import configparser
import json
import re
import socket
import struct
from pathlib import Path

from ..registry import Tool, ToolContext, ToolRegistry, fail, ok

# ============================================================ tabular data (chunked, bounded memory)


def _csv_chunks(path: Path, size: int = 100_000):
    import pandas as pd
    sep = "\t" if path.suffix.lower() == ".tsv" else ","
    yield from pd.read_csv(path, chunksize=size, sep=sep, on_bad_lines="skip", dtype=str, encoding_errors="replace")


def _read_frame_chunks(path: Path):
    ext = path.suffix.lower()
    if ext in (".csv", ".tsv"):
        yield from _csv_chunks(path)
    elif ext in (".jsonl", ".ndjson"):
        import pandas as pd
        yield from pd.read_json(path, lines=True, chunksize=100_000, dtype=False)
    elif ext == ".parquet":
        import pyarrow.parquet as pq
        for b in pq.ParquetFile(path).iter_batches(batch_size=100_000):
            yield b.to_pandas()
    elif ext in (".xlsx", ".xlsm"):
        import pandas as pd
        yield pd.read_excel(path)
    elif ext == ".json":
        import pandas as pd
        yield pd.read_json(path)
    else:
        raise ValueError(f"unsupported data file type '{ext}'")


def csv_inspect(ctx: ToolContext, a: dict) -> dict:
    p = ctx.app.perms.paths.resolve(a["path"])
    n = int(a.get("rows", 5))
    first = None
    total = 0
    nulls: dict[str, int] = collections.Counter()
    for ch in _read_frame_chunks(p):
        if first is None:
            first = ch.head(n)
        total += len(ch)
        for c, v in ch.isna().sum().items():
            nulls[str(c)] += int(v)
    if first is None:
        return fail("file contains no rows")
    return ok(path=str(p), rows=total, columns=[str(c) for c in first.columns], null_counts=dict(nulls),
              preview=json.loads(first.to_json(orient="records")))


def data_analyze(ctx: ToolContext, a: dict) -> dict:
    import pandas as pd
    p = ctx.app.perms.paths.resolve(a["path"])
    op, col = a["operation"], a.get("column")
    by, how = a.get("group_by"), a.get("agg", "count")
    limit_rows = int(a.get("max_rows", 5_000_000))
    seen = 0
    counter: collections.Counter = collections.Counter()
    acc: dict = {}
    for ch in _read_frame_chunks(p):
        if seen >= limit_rows:
            break
        seen += len(ch)
        if op == "value_counts":
            if col not in ch.columns:
                return fail(f"column '{col}' not found; columns: {list(ch.columns)[:30]}")
            counter.update(ch[col].dropna().astype(str).tolist())
        elif op == "describe":
            if col not in ch.columns:
                return fail(f"column '{col}' not found")
            x = pd.to_numeric(ch[col], errors="coerce").dropna()
            if len(x):
                acc["n"] = acc.get("n", 0) + len(x)
                acc["sum"] = acc.get("sum", 0.0) + float(x.sum())
                acc["sumsq"] = acc.get("sumsq", 0.0) + float((x ** 2).sum())
                acc["min"] = min(acc.get("min", float("inf")), float(x.min()))
                acc["max"] = max(acc.get("max", float("-inf")), float(x.max()))
        elif op == "groupby":
            if col not in ch.columns or by not in ch.columns:
                return fail(f"columns not found; available: {list(ch.columns)[:30]}")
            vals = pd.to_numeric(ch[col], errors="coerce")
            g = pd.DataFrame({"k": ch[by].astype(str), "v": vals}).groupby("k")["v"].agg(["count", "sum", "min", "max"])
            for k, r in g.iterrows():
                cur = acc.setdefault(k, {"count": 0, "sum": 0.0, "min": float("inf"), "max": float("-inf")})
                cur["count"] += int(r["count"])
                cur["sum"] += float(r["sum"])
                cur["min"] = min(cur["min"], float(r["min"])) if r["count"] else cur["min"]
                cur["max"] = max(cur["max"], float(r["max"])) if r["count"] else cur["max"]
        elif op == "filter_count":
            if col not in ch.columns:
                return fail(f"column '{col}' not found")
            val = str(a.get("value", ""))
            s = ch[col].astype(str)
            m = {"eq": s == val, "ne": s != val, "contains": s.str.contains(val, regex=False, na=False)}.get(a.get("op", "eq"))
            if m is None:
                return fail("op must be eq, ne or contains")
            acc["matches"] = acc.get("matches", 0) + int(m.sum())
        elif op == "head":
            return ok(rows=json.loads(ch.head(int(a.get("n", 10))).to_json(orient="records")))
        else:
            return fail("operation must be one of value_counts, describe, groupby, filter_count, head")
    if op == "value_counts":
        return ok(rows_scanned=seen, top=[{"value": k, "count": v} for k, v in counter.most_common(int(a.get("top", 20)))],
                  distinct=len(counter))
    if op == "describe":
        if not acc:
            return fail("no numeric values found")
        mean = acc["sum"] / acc["n"]
        var = max(0.0, acc["sumsq"] / acc["n"] - mean ** 2)
        return ok(rows_scanned=seen, count=acc["n"], mean=mean, std=var ** 0.5, min=acc["min"], max=acc["max"])
    if op == "groupby":
        out = [{"group": k, "count": v["count"], "sum": v["sum"], "mean": v["sum"] / v["count"] if v["count"] else None,
                "min": v["min"], "max": v["max"]} for k, v in acc.items()]
        key = {"count": "count", "sum": "sum", "mean": "mean", "min": "min", "max": "max"}.get(how, "count")
        out.sort(key=lambda r: -(r[key] or 0))
        return ok(rows_scanned=seen, groups=out[:int(a.get("top", 20))], total_groups=len(out))
    return ok(rows_scanned=seen, **acc)


def json_query(ctx: ToolContext, a: dict) -> dict:
    p = ctx.app.perms.paths.resolve(a["path"])
    if p.stat().st_size > 50 * 2**20:
        return fail("file too large for json_query (50 MB limit); use data_analyze on JSONL")
    cur = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    for part in [x for x in re.split(r"\.|\[(\d+)\]", a.get("query", "")) if x]:
        try:
            cur = cur[int(part)] if isinstance(cur, list) else cur[part]
        except (KeyError, IndexError, ValueError, TypeError):
            return fail(f"path segment '{part}' not found")
    s = json.dumps(cur, default=str)
    return ok(value=cur if len(s) < 8000 else s[:8000] + "...", truncated=len(s) >= 8000, type=type(cur).__name__)


# ============================================================ code / config / logs
SECRET_RX = re.compile(r"""(?ix)(?:api[_-]?key|secret|passwd|password|token|private[_-]?key|access[_-]?key)\s*[:=]\s*['"]?([A-Za-z0-9_\-/+=.@!#$%^&*]{8,})""")
RISKY_CALLS = {"py": [(r"\beval\(", "eval() executes arbitrary code"), (r"\bexec\(", "exec() executes arbitrary code"),
                      (r"shell\s*=\s*True", "subprocess with shell=True enables command injection"),
                      (r"pickle\.loads?\(", "unpickling untrusted data can execute code"), (r"yaml\.load\((?!.*Loader)", "yaml.load without a safe Loader"),
                      (r"verify\s*=\s*False", "TLS verification disabled"), (r"except\s*:\s*$", "bare except hides errors"),
                      (r"hashlib\.(md5|sha1)\(", "weak hash function")],
               "generic": [(r"\bstrcpy\(|\bgets\(|\bsprintf\(", "unsafe C string function"), (r"innerHTML\s*=", "innerHTML assignment (XSS risk)"),
                           (r"\beval\(", "eval()"), (r"\bsystem\(", "system() call"), (r"Invoke-Expression|\biex\b", "PowerShell Invoke-Expression"),
                           (r"verify\s*[:=]\s*false", "TLS verification disabled")]}


def _read_text_arg(ctx: ToolContext, a: dict) -> tuple[str, str]:
    if a.get("code") is not None:
        return a["code"], "<inline>"
    p = ctx.app.perms.paths.resolve(a["path"])
    if p.stat().st_size > 5 * 2**20:
        raise ValueError("file larger than 5 MB; analyse a smaller part")
    return p.read_text(encoding="utf-8", errors="replace"), str(p)


def code_analyze(ctx: ToolContext, a: dict) -> dict:
    text, src = _read_text_arg(ctx, a)
    lines = text.splitlines()
    findings = []
    lang = "py" if (src.endswith(".py") or a.get("language") == "python") else "generic"
    for i, ln in enumerate(lines, 1):
        for rx, why in RISKY_CALLS[lang]:
            if re.search(rx, ln):
                findings.append({"line": i, "severity": "medium", "issue": why, "evidence": ln.strip()[:160], "status": "confirmed-pattern"})
        m = SECRET_RX.search(ln)
        if m and not re.search(r"os\.environ|getenv|\$\{|<.*>|example|changeme|your_", ln, re.I):
            findings.append({"line": i, "severity": "high", "issue": "possible hard-coded secret", "evidence": SECRET_RX.sub(lambda mm: mm.group(0)[:mm.start(1) - mm.start(0)] + "***", ln).strip()[:160],
                             "status": "hypothesis (needs human check)"})
        if re.search(r"\b(TODO|FIXME|HACK|XXX)\b", ln):
            findings.append({"line": i, "severity": "info", "issue": "unfinished work marker", "evidence": ln.strip()[:120], "status": "confirmed-pattern"})
    summary: dict = {"source": src, "lines": len(lines), "language": lang}
    if lang == "py":
        try:
            tree = ast.parse(text)
            funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            summary["functions"] = len(funcs)
            summary["classes"] = sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree))
            summary["imports"] = sorted({(n.module or "") if isinstance(n, ast.ImportFrom) else al.name for n in ast.walk(tree)
                                         if isinstance(n, (ast.Import, ast.ImportFrom)) for al in (n.names if isinstance(n, ast.Import) else [n])})[:50]
            for f in funcs:
                branches = sum(isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.BoolOp, ast.ExceptHandler)) for n in ast.walk(f))
                length = (f.end_lineno or f.lineno) - f.lineno + 1
                if branches > 12 or length > 80:
                    findings.append({"line": f.lineno, "severity": "low", "issue": f"function '{f.name}' is complex (branches={branches}, lines={length})",
                                     "evidence": f"def {f.name}", "status": "confirmed-metric"})
        except SyntaxError as e:
            findings.append({"line": e.lineno or 0, "severity": "high", "issue": f"syntax error: {e.msg}", "evidence": (e.text or "").strip()[:120], "status": "confirmed"})
    return ok(summary=summary, findings=findings[:100], total_findings=len(findings),
              note="'confirmed-pattern' = the pattern is present in the text; whether it is exploitable depends on context.")


def _flatten(o, p=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _flatten(v, f"{p}{k}.")
    elif isinstance(o, list):
        for i, v in enumerate(o[:100]):
            yield from _flatten(v, f"{p}{i}.")
    else:
        yield p.rstrip("."), o


NET_RULES = [(r"^\s*enable password\b", "plaintext enable password"), (r"snmp-server community (public|private)\b", "default SNMP community string"),
             (r"^\s*transport input .*telnet", "telnet enabled on vty lines"), (r"^\s*ip http server", "HTTP management server enabled"),
             (r"permit ip any any", "overly permissive ACL (permit ip any any)"), (r"^\s*no service password-encryption", "password encryption disabled")]


def config_analyze(ctx: ToolContext, a: dict) -> dict:
    text, src = _read_text_arg(ctx, a)
    ext = Path(src).suffix.lower()
    flat: list[tuple[str, object]] = []
    findings = []
    try:
        if ext == ".json":
            flat = list(_flatten(json.loads(text)))
        elif ext in (".yaml", ".yml"):
            import yaml
            flat = list(_flatten(yaml.safe_load(text)))
        elif ext == ".toml":
            import tomllib
            flat = list(_flatten(tomllib.loads(text)))
        elif ext in (".ini", ".cfg", ".conf") and "=" in text:
            cp = configparser.ConfigParser(strict=False, interpolation=None)
            cp.read_string(text if "[" in text else "[default]\n" + text)
            flat = [(f"{s}.{k}", v) for s in cp.sections() for k, v in cp[s].items()]
        elif ext in (".env", "") or Path(src).name.startswith(".env"):
            flat = [tuple(l.split("=", 1)) for l in text.splitlines() if "=" in l and not l.strip().startswith("#")]  # type: ignore[misc]
    except Exception as e:  # noqa: BLE001
        findings.append({"severity": "high", "issue": f"configuration does not parse: {e}", "status": "confirmed"})
    for k, v in flat:
        sv, kl = str(v), str(k).lower()
        if re.search(r"pass(word|wd)?|secret|token|api[_-]?key|private[_-]?key", kl) and sv and not re.match(r"^(\$\{|<|%|\*+$|changeme|example)", sv, re.I):
            findings.append({"key": k, "severity": "high", "issue": "credential-like value stored in config", "evidence": f"{k} = ***", "status": "confirmed-pattern"})
        if re.search(r"debug", kl) and sv.lower() in ("true", "1", "yes", "on"):
            findings.append({"key": k, "severity": "medium", "issue": "debug mode enabled", "evidence": f"{k} = {sv}", "status": "confirmed-pattern"})
        if sv.strip() in ("0.0.0.0", "::") or re.search(r"bind|listen|host", kl) and "0.0.0.0" in sv:
            findings.append({"key": k, "severity": "medium", "issue": "listens on all interfaces", "evidence": f"{k} = {sv}", "status": "confirmed-pattern"})
        if re.search(r"verify|ssl|tls", kl) and sv.lower() in ("false", "0", "no", "off", "none"):
            findings.append({"key": k, "severity": "high", "issue": "TLS/SSL verification disabled", "evidence": f"{k} = {sv}", "status": "confirmed-pattern"})
        if re.search(r"cors|origin", kl) and sv.strip() == "*":
            findings.append({"key": k, "severity": "medium", "issue": "wildcard CORS origin", "evidence": f"{k} = *", "status": "confirmed-pattern"})
        if re.search(r"user(name)?$", kl) and sv.lower() in ("admin", "root", "administrator") and any(re.search("pass", str(kk).lower()) for kk, _ in flat):
            findings.append({"key": k, "severity": "low", "issue": "default-looking administrative account name", "evidence": f"{k} = {sv}", "status": "hypothesis"})
    for i, ln in enumerate(text.splitlines(), 1):
        for rx, why in NET_RULES:
            if re.search(rx, ln, re.I):
                findings.append({"line": i, "severity": "medium", "issue": why, "evidence": ln.strip()[:120], "status": "confirmed-pattern"})
    return ok(source=src, keys_parsed=len(flat), findings=findings[:100], total_findings=len(findings))


_LEVEL = re.compile(r"\b(CRITICAL|FATAL|ERROR|ERR|WARN(?:ING)?|INFO|DEBUG|TRACE)\b", re.I)
_TS = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)")
_NORM = [(re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
         (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"), (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
         (re.compile(r"\d+"), "<n>")]


def log_analyze(ctx: ToolContext, a: dict) -> dict:
    p = ctx.app.perms.paths.resolve(a["path"])
    lv: collections.Counter = collections.Counter()
    sigs: collections.Counter = collections.Counter()
    sample: dict[str, tuple[int, str]] = {}
    per_min: collections.Counter = collections.Counter()
    first = last = None
    n = 0
    needle = re.compile(a["pattern"], re.I) if a.get("pattern") else None
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if n > int(a.get("max_lines", 2_000_000)):
                break
            m = _LEVEL.search(line)
            level = m.group(1).upper() if m else "UNKNOWN"
            level = {"ERR": "ERROR", "WARN": "WARNING"}.get(level, level)
            lv[level] += 1
            t = _TS.search(line)
            if t:
                first = first or t.group(1)
                last = t.group(1)
                if level in ("ERROR", "CRITICAL", "FATAL"):
                    per_min[t.group(1)[:16]] += 1
            if level in ("ERROR", "CRITICAL", "FATAL", "WARNING") or (needle and needle.search(line)):
                sig = line.strip()
                sig = _TS.sub("", sig)
                for rx, rep in _NORM:
                    sig = rx.sub(rep, sig)
                sig = sig[:160]
                sigs[sig] += 1
                sample.setdefault(sig, (n, line.strip()[:200]))
    burst = per_min.most_common(1)
    return ok(path=str(p), lines=n, levels=dict(lv), time_range=[first, last],
              top_signatures=[{"signature": s, "count": c, "first_line": sample[s][0], "example": sample[s][1]} for s, c in sigs.most_common(int(a.get("top", 10)))],
              error_burst={"minute": burst[0][0], "errors": burst[0][1]} if burst else None,
              note="Signatures group messages after masking numbers/ids. Counts are facts; causes are hypotheses until verified.")


# ============================================================ packet captures
def _pcap_packets(path: Path, limit: int):
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
            e = ">"
        elif magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
            e = "<"
        elif magic == b"\x0a\x0d\x0d\x0a":
            yield from _pcapng(path, limit)
            return
        else:
            raise ValueError("not a pcap/pcapng file")
        nano = magic in (b"\xa1\xb2\x3c\x4d", b"\x4d\x3c\xb2\xa1")
        hdr = f.read(20)
        link = struct.unpack(e + "I", hdr[16:20])[0]
        count = 0
        while count < limit:
            ph = f.read(16)
            if len(ph) < 16:
                break
            sec, frac, incl, orig = struct.unpack(e + "IIII", ph)
            data = f.read(incl)
            yield sec + frac / (1e9 if nano else 1e6), link, data, orig
            count += 1


def _pcapng(path: Path, limit: int):
    with open(path, "rb") as f:
        e = "<"
        links: list[int] = []
        count = 0
        while count < limit:
            h = f.read(8)
            if len(h) < 8:
                break
            btype = struct.unpack("<I", h[:4])[0]
            blen = struct.unpack("<I", h[4:])[0]
            body = f.read(max(0, blen - 8))
            if btype == 0x0A0D0D0A:
                e = "<" if body[:4] == b"\x4d\x3c\x2b\x1a" else ">"
            elif btype == 1:
                links.append(struct.unpack(e + "H", body[:2])[0])
            elif btype == 6:
                iface, ts_hi, ts_lo, cap, orig = struct.unpack(e + "IIIII", body[:20])
                yield ((ts_hi << 32) | ts_lo) / 1e6, links[iface] if iface < len(links) else 1, body[20:20 + cap], orig
                count += 1


def _dissect(link: int, data: bytes) -> dict | None:
    off = 14 if link == 1 else 0 if link == 101 else None
    if off is None or len(data) < off + 20:
        return None
    etype = struct.unpack(">H", data[12:14])[0] if link == 1 else 0x0800
    if link == 1 and etype == 0x8100:
        off, etype = 18, struct.unpack(">H", data[16:18])[0]
    if etype == 0x0806:
        return {"proto": "ARP"}
    if etype == 0x0800:
        ihl = (data[off] & 0xF) * 4
        proto = data[off + 9]
        src, dst = socket.inet_ntoa(data[off + 12:off + 16]), socket.inet_ntoa(data[off + 16:off + 20])
        t = data[off + ihl:]
        d = {"src": src, "dst": dst, "proto": {1: "ICMP", 6: "TCP", 17: "UDP"}.get(proto, f"IP/{proto}")}
        if proto in (6, 17) and len(t) >= 8:
            d["sport"], d["dport"] = struct.unpack(">HH", t[:4])
            if proto == 6 and len(t) >= 14:
                d["flags"] = t[13]
        return d
    if etype == 0x86DD and len(data) >= off + 40:
        nxt = data[off + 6]
        d = {"src": socket.inet_ntop(socket.AF_INET6, data[off + 8:off + 24]), "dst": socket.inet_ntop(socket.AF_INET6, data[off + 24:off + 40]),
             "proto": {6: "TCP", 17: "UDP", 58: "ICMPv6"}.get(nxt, f"IPv6/{nxt}")}
        t = data[off + 40:]
        if nxt in (6, 17) and len(t) >= 4:
            d["sport"], d["dport"] = struct.unpack(">HH", t[:4])
            if nxt == 6 and len(t) >= 14:
                d["flags"] = t[13]
        return d
    return {"proto": f"ethertype/0x{etype:04x}"}


def pcap_summary(ctx: ToolContext, a: dict) -> dict:
    p = ctx.app.perms.paths.resolve(a["path"])
    protos: collections.Counter = collections.Counter()
    talkers: collections.Counter = collections.Counter()
    ports: collections.Counter = collections.Counter()
    syn_no_ack: collections.Counter = collections.Counter()
    t0 = t1 = None
    n = byte = 0
    try:
        for ts, link, data, orig in _pcap_packets(p, int(a.get("max_packets", 500_000))):
            n += 1
            byte += orig
            t0 = ts if t0 is None else t0
            t1 = ts
            d = _dissect(link, data)
            if not d:
                protos["unparsed"] += 1
                continue
            protos[d["proto"]] += 1
            if "src" in d:
                talkers[f"{d['src']} -> {d['dst']}"] += 1
            if "dport" in d:
                ports[f"{d['proto']}/{d['dport']}"] += 1
            if d.get("proto") == "TCP" and d.get("flags", 0) & 0x12 == 0x02:
                syn_no_ack[d["src"]] += 1
    except ValueError as e:
        return fail(str(e) + ". Supported: classic pcap and pcapng (Ethernet/raw-IP). Export other formats with tshark/Wireshark.")
    hyp = [{"hypothesis": f"{ip} sent {c} TCP SYNs without handshake completion (possible scan or unreachable service)", "confidence": "low",
            "evidence": f"{c} SYN-only packets"} for ip, c in syn_no_ack.most_common(3) if c >= 50]
    return ok(path=str(p), packets=n, bytes=byte, duration_s=round((t1 or 0) - (t0 or 0), 3), protocols=dict(protos),
              top_conversations=[{"flow": k, "packets": v} for k, v in talkers.most_common(10)],
              top_destination_ports=[{"port": k, "packets": v} for k, v in ports.most_common(10)], hypotheses=hyp,
              note="Counts are measured facts. Hypotheses are heuristics and must be confirmed against the capture in a proper analyzer.")


def register(reg: ToolRegistry) -> None:
    S = lambda **p: {"type": "object", "properties": p, "additionalProperties": False}  # noqa: E731
    reg.register(Tool("csv_inspect", "Inspect a tabular data file (csv/tsv/jsonl/parquet/xlsx): row count, columns, null counts, preview.",
                      {**S(path={"type": "string"}, rows={"type": "integer"}), "required": ["path"]}, csv_inspect, dependencies=["pandas"], timeout=300))
    reg.register(Tool("data_analyze", "Aggregate a tabular file in streaming chunks: value_counts, describe, groupby, filter_count, head.",
                      {**S(path={"type": "string"}, operation={"type": "string", "enum": ["value_counts", "describe", "groupby", "filter_count", "head"]},
                           column={"type": "string"}, group_by={"type": "string"}, agg={"type": "string", "enum": ["count", "sum", "mean", "min", "max"]},
                           op={"type": "string", "enum": ["eq", "ne", "contains"]}, value={"type": "string"}, top={"type": "integer"},
                           n={"type": "integer"}, max_rows={"type": "integer"}), "required": ["path", "operation"]},
                      data_analyze, dependencies=["pandas"], timeout=600))
    reg.register(Tool("json_query", "Extract a value from a JSON file using a dotted path like a.b[0].c.",
                      {**S(path={"type": "string"}, query={"type": "string"}), "required": ["path", "query"]}, json_query))
    reg.register(Tool("code_analyze", "Static analysis of source code (file or inline): structure, complexity, risky calls, hard-coded secrets.",
                      S(path={"type": "string"}, code={"type": "string"}, language={"type": "string"}), code_analyze, untrusted_output=True))
    reg.register(Tool("config_analyze", "Analyze a configuration file (json/yaml/toml/ini/env/network device config) for insecure settings.",
                      S(path={"type": "string"}, code={"type": "string"}), config_analyze, untrusted_output=True))
    reg.register(Tool("log_analyze", "Summarize a log file: levels, time range, grouped error signatures, error bursts.",
                      {**S(path={"type": "string"}, pattern={"type": "string"}, top={"type": "integer"}, max_lines={"type": "integer"}), "required": ["path"]},
                      log_analyze, untrusted_output=True, timeout=300))
    reg.register(Tool("pcap_summary", "Summarize a pcap/pcapng capture: protocols, top conversations and ports, scan-like heuristics.",
                      {**S(path={"type": "string"}, max_packets={"type": "integer"}), "required": ["path"]}, pcap_summary, timeout=300))
