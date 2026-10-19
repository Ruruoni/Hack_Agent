"""Deterministic chunk-level quality checks. Returns ('accept'|'reject'|'quarantine', reason)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..knowledge.text import scan_injection

_WORD = re.compile(r"\w+", re.U)
_BOILER = re.compile(r"(lorem ipsum dolor|cookie(s)? (policy|settings|consent)|accept all cookies|subscribe to our newsletter|"
                     r"all rights reserved\.?\s*$)", re.I)


@dataclass
class QualityRules:
    min_chars: int = 40
    max_symbol_ratio: float = 0.55
    min_unique_ratio: float = 0.12        # unique tokens / tokens, applied to chunks > 200 chars
    max_replacement_ratio: float = 0.02
    quarantine_injections: bool = True
    include_patterns: list[str] = field(default_factory=list)   # relevance: must match at least one (if given)
    exclude_patterns: list[str] = field(default_factory=list)   # reject when any matches
    structured: bool = False                                      # tabular rows: relax length/symbol rules

    def compiled(self):
        return ([re.compile(p, re.I) for p in self.include_patterns], [re.compile(p, re.I) for p in self.exclude_patterns])


def assess(text: str, rules: QualityRules, inc=None, exc=None) -> tuple[str, str]:
    n = len(text)
    if n < (10 if rules.structured else rules.min_chars):
        return "reject", f"low-information: only {n} characters"
    rep = text.count("�")
    if rep and rep / n > rules.max_replacement_ratio:
        return "reject", f"encoding problems: {rep} replacement characters"
    toks = _WORD.findall(text.lower())
    if not toks:
        return "reject", "no word content"
    if not rules.structured:
        sym = sum(1 for ch in text if not (ch.isalnum() or ch.isspace()))
        if sym / n > rules.max_symbol_ratio:
            return "reject", f"mostly symbols/noise ({sym / n:.0%})"
    if n > 200 and len(toks) > 20 and len(set(toks)) / len(toks) < rules.min_unique_ratio:
        return "reject", "suspicious repetition (very low unique-token ratio)"
    if exc is None:
        inc, exc = rules.compiled()
    for rx in exc:
        if rx.search(text):
            return "reject", f"excluded by filter /{rx.pattern}/"
    if inc and not any(rx.search(text) for rx in inc):
        return "reject", "irrelevant: matches none of the include filters"
    if _BOILER.search(text) and n < 400:
        return "reject", "boilerplate (cookie banner / lorem ipsum / footer)"
    if rules.quarantine_injections:
        flags = scan_injection(text)
        if flags:
            return "quarantine", "possible embedded instructions: " + ", ".join(flags)
    return "accept", ""
