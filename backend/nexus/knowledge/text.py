"""Text utilities: normalization, chunking, hashing, simhash, injection scan, fact extraction."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import zlib
from typing import Iterator

_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")
_WORD = re.compile(r"\w+", re.U)


def normalize(text: str) -> str:
    """Conservative cleanup: unicode NFC, strip control chars/NULs, collapse runs of whitespace.
    Never removes words, numbers, punctuation, or line structure (code/logs keep their newlines)."""
    text = unicodedata.normalize("NFC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    text = _WS.sub(" ", text)
    text = "\n".join(l.rstrip() for l in text.split("\n"))
    return _NL.sub("\n\n", text).strip()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def canon(text: str) -> str:
    """Canonical form used for exact-duplicate detection (case/space-insensitive)."""
    return " ".join(_WORD.findall(text.lower()))


def simhash64(text: str) -> int:
    toks = _WORD.findall(text.lower())
    if not toks:
        return 0
    shingles = [" ".join(toks[i:i + 3]) for i in range(max(1, len(toks) - 2))]
    acc = [0] * 64
    for s in shingles:
        h = int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")
        for b in range(64):
            acc[b] += 1 if (h >> b) & 1 else -1
    out = 0
    for b in range(64):
        if acc[b] > 0:
            out |= 1 << b
    return out


def to_signed(x: int) -> int:
    return x - (1 << 64) if x >= (1 << 63) else x


def to_unsigned(x: int) -> int:
    return x + (1 << 64) if x < 0 else x


def bands(h: int) -> tuple[int, int, int, int]:
    return (h & 0xFFFF, (h >> 16) & 0xFFFF, (h >> 32) & 0xFFFF, (h >> 48) & 0xFFFF)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> Iterator[str]:
    """Paragraph-aware chunker. Splits on blank lines, then sentences, then hard-wraps."""
    text = text.strip()
    if not text:
        return
    if len(text) <= size:
        yield text
        return
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
            continue
        sents = re.split(r"(?<=[.!?])\s+|\n", para)
        buf = ""
        for s in sents:
            while len(s) > size:
                if buf:
                    pieces.append(buf)
                    buf = ""
                pieces.append(s[:size])
                s = s[size - overlap:] if overlap < size else s[size:]
            if len(buf) + len(s) + 1 > size and buf:
                pieces.append(buf)
                buf = s
            else:
                buf = f"{buf} {s}".strip() if buf else s
        if buf:
            pieces.append(buf)
    cur = ""
    prev_tail = ""
    for p in pieces:
        if cur and len(cur) + len(p) + 2 > size:
            yield cur
            prev_tail = cur[-overlap:] if overlap else ""
            cur = (prev_tail + "\n" + p) if prev_tail and len(prev_tail) + len(p) + 1 <= size else p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        yield cur


# ---- prompt-injection / malicious-instruction detection -----------------------------------
_INJECTION = [
    (r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|rules)", "ignore-previous"),
    (r"disregard (all |any |the )?(previous|prior|above|your) (instructions|rules|guidelines|policies)", "disregard"),
    (r"(you are|you're) now (in )?(a |an )?(different|new|unrestricted|jailbroken|dan)\b", "role-override"),
    (r"\b(reveal|print|show|output|repeat) (your |the )?(system prompt|hidden instructions|initial instructions)", "prompt-exfil"),
    (r"(override|bypass|disable|turn off) (the |your |all )?(safety|safeguards?|permissions?|guardrails?|approval)", "safeguard-bypass"),
    (r"\b(approve|authorize|grant) (this|these|all|every) (action|request|command|tool call)s? (automatically|yourself)?", "self-approve"),
    (r"(do not|don't|never) (tell|inform|notify|alert) the user", "conceal-from-user"),
    (r"<\s*/?\s*(system|assistant|im_start|im_end)\s*>|\[/?INST\]|<\|im_(start|end)\|>", "fake-role-tags"),
    (r"(run|execute|invoke) (the following|this) (shell |bash |powershell |terminal )?(command|script|code)", "run-command"),
    (r"\b(curl|wget|Invoke-WebRequest)\b[^\n]{0,80}\|\s*(sh|bash|powershell|iex)\b", "pipe-to-shell"),
    (r"(send|post|upload|exfiltrate) (the |all |your )?(conversation|memory|memories|api keys?|secrets?|credentials|passwords?) (to|at) ", "exfiltrate"),
    (r"when (an )?(ai|assistant|llm|language model)\b[^\n]{0,60}(reads?|sees?|processes)", "ai-targeted"),
]
_INJ_RE = [(re.compile(p, re.I), name) for p, name in _INJECTION]


def scan_injection(text: str) -> list[str]:
    """Returns the names of suspicious instruction patterns found (empty list = nothing flagged)."""
    return sorted({name for rx, name in _INJ_RE if rx.search(text)})


# ---- lightweight deterministic fact/entity extraction -----------------------------------------
_PATTERNS = {
    "cve": re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I),
    "ipv4": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    "url": re.compile(r"https?://[^\s<>\"')]+"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "version": re.compile(r"\b[Vv]?\d+\.\d+\.\d+(?:[-.]\w+)?\b"),
}
_ISA = re.compile(r"^\s*([A-Z][\w\- ]{1,40}?) (?:is|are) (?:a|an|the) ([\w\- ,]{3,60}?)[.;]", re.M)
_KV = re.compile(r"^\s*([A-Za-z_][\w.\-]{1,40})\s*[:=]\s*(\S[^\n]{0,80})$", re.M)


def extract_facts(text: str, limit: int = 12) -> list[tuple[str, str, str]]:
    """(subject, predicate, object) triples. Deterministic; confidence is always low ('extracted')."""
    out: list[tuple[str, str, str]] = []
    for kind, rx in _PATTERNS.items():
        for m in list(dict.fromkeys(rx.findall(text)))[:4]:
            out.append(("chunk", f"mentions_{kind}", m))
    for m in _ISA.finditer(text):
        out.append((m.group(1).strip(), "is_a", m.group(2).strip()))
    kv = [(k, v.strip()) for k, v in _KV.findall(text)]
    if 2 <= len(kv) <= 40:
        out += [("config", k, v) for k, v in kv[:6]]
    return out[:limit]
