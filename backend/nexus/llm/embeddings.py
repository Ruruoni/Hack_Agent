"""Embedding providers. Every collection records the embedder id it was built with; vectors from
different embedders are never mixed (a mismatch triggers keyword-only retrieval + a re-index prompt)."""
from __future__ import annotations

import re
import zlib
from abc import ABC, abstractmethod

import numpy as np

from .base import LLMError, LLMProvider

_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = frozenset("a an the and or of to in on for with is are was were be been it this that as at by from".split())


class Embedder(ABC):
    id: str
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashEmbedder(Embedder):
    """Deterministic, dependency-free, offline. Hashed bag of words + bigrams + char trigrams.

    This is LEXICAL similarity (shared vocabulary), not deep semantics. It exists so the app works
    out of the box; configure an LM Studio embedding model for true semantic retrieval."""

    def __init__(self, dim: int = 384):
        self.dim = dim
        self.id = f"hash-{dim}"

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        toks = [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]
        feats: list[tuple[str, float]] = [(t, 1.0) for t in toks]
        feats += [(a + "_" + b, 0.7) for a, b in zip(toks, toks[1:])]
        for t in toks:
            if len(t) > 4:
                feats += [("#" + t[i:i + 3], 0.25) for i in range(len(t) - 2)]
        for f, w in feats:
            h = zlib.crc32(f.encode())
            v[h % self.dim] += w if (h >> 16) & 1 else -w
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)


class LMStudioEmbedder(Embedder):
    def __init__(self, provider: LLMProvider, model: str, batch: int = 32):
        self.provider, self.model, self.batch = provider, model, batch
        self.id = f"lmstudio:{model}"
        self.dim = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            out += self.provider.embed(texts[i:i + self.batch], model=self.model or None)
        arr = np.asarray(out, dtype=np.float32)
        if arr.size:
            self.dim = arr.shape[1]
            n = np.linalg.norm(arr, axis=1, keepdims=True)
            arr = arr / np.where(n == 0, 1, n)
        return arr


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model: str):
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as e:
            raise LLMError("missing_dependency", "sentence-transformers is not installed.",
                           "pip install sentence-transformers  (large: pulls PyTorch)") from e
        self.m = SentenceTransformer(model)
        self.id = f"st:{model}"
        self.dim = int(self.m.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.m.encode(texts, normalize_embeddings=True), dtype=np.float32)


def build_embedder(settings, provider: LLMProvider) -> Embedder:
    kind = settings.embedding_provider
    if kind == "lmstudio":
        return LMStudioEmbedder(provider, settings.embedding_model, settings.embedding_batch_size)
    if kind == "sentence_transformers":
        return SentenceTransformerEmbedder(settings.embedding_model)
    return HashEmbedder()
