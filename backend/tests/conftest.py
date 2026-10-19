from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_lmstudio import MockLM  # noqa: E402

from nexus.config import load_settings  # noqa: E402
from nexus.core import AppContext  # noqa: E402
from nexus.llm.embeddings import HashEmbedder  # noqa: E402
from nexus.llm.lmstudio import LMStudioProvider  # noqa: E402


@pytest.fixture()
def settings(tmp_path):
    s = load_settings(str(tmp_path / "data"))
    s.vector_backend = "sqlite"       # fast; Chroma has its own parametrized tests
    s.web_allow_private = True        # tests serve pages from 127.0.0.1
    s.web_rate_limit_s = 0
    s.min_sample_for_gate = 10_000    # quality gate tests lower this explicitly
    return s


@pytest.fixture()
def mock_lm():
    m = MockLM().start()
    yield m
    m.stop()


@pytest.fixture()
def mock_lm_native():
    m = MockLM(native_tools=True).start()
    yield m
    m.stop()


@pytest.fixture()
def ctx(settings):
    """App context with the real LM Studio provider pointed at nothing; offline hash embeddings."""
    c = AppContext(settings, provider=LMStudioProvider("http://127.0.0.1:9/v1", timeout=2), embedder=HashEmbedder())
    return c


@pytest.fixture()
def lm_ctx(settings, mock_lm):
    settings.lmstudio_base_url = mock_lm.base_url
    c = AppContext(settings, embedder=HashEmbedder())
    return c


def write(tmp_path: Path, name: str, content: str | bytes) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p
