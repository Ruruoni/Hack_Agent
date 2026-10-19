"""Application settings.

Resolution order (later wins):  defaults  <  .env / environment (NEXUS_*)  <  data/settings.json

`settings.json` is written ONLY by the user-facing settings API. The agent has no tool that can
read or write it, which is how privilege/permission settings stay outside the language model's reach.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

EXECUTION_MODES = ("read_only", "approval_required", "restricted_automation")

# Settings that only the human (via the settings API) may change. The agent never gets a handle to them.
SECRET_FIELDS = {"lmstudio_api_key", "search_api_key"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():   # utf-8-sig: Windows Notepad adds a BOM
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Settings:
    # --- storage -------------------------------------------------------------------------
    data_dir: str = ""                       # resolved in load(); default <repo>/data
    # --- LM Studio / LLM -----------------------------------------------------------------
    llm_provider: str = "lmstudio"
    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_api_key: str = ""               # only needed if the LM Studio server requires one
    chat_model: str = ""                     # empty = first loaded/available chat model
    context_window: int = 8192               # tokens we budget for a prompt (NOT forced on LM Studio)
    temperature: float = 0.4
    top_p: float = 0.95
    max_tokens: int = 1024
    request_timeout: float = 120.0
    tool_mode: str = "auto"                  # auto | native | prompt  (prompt = JSON-action fallback)
    # --- embeddings ----------------------------------------------------------------------
    embedding_provider: str = "hash"         # hash | lmstudio | sentence_transformers
    embedding_model: str = ""                # e.g. text-embedding-nomic-embed-text-v1.5
    embedding_batch_size: int = 32
    vector_backend: str = "auto"             # auto | chroma | sqlite
    # --- conversation ---------------------------------------------------------------------
    personality: str = "friendly, precise, and concise"
    response_length: str = "medium"          # short | medium | long
    explain_mode: str = "technical"          # technical | beginner
    history_messages: int = 8                # raw recent messages sent to the model
    summarize_after: int = 14                # messages before a rolling summary is generated
    retrieval_top_k: int = 5
    # --- permissions ---------------------------------------------------------------------
    execution_mode: str = "approval_required"
    allowed_roots: list[str] = field(default_factory=list)   # extra dirs the agent may touch
    auto_allow_tools: list[str] = field(default_factory=lambda: [
        "knowledge_search", "memory_search", "fs_list", "fs_read", "csv_inspect",
        "data_analyze", "json_query", "code_analyze", "config_analyze", "log_analyze",
        "pcap_summary", "system_diagnostics", "report_generate", "db_query"])
    authorized_targets: list[str] = field(default_factory=list)  # hosts/CIDRs for network tools
    approval_timeout_s: int = 900
    read_only_allows_network: bool = True
    # --- agent limits ---------------------------------------------------------------------
    max_task_steps: int = 12
    max_tool_calls: int = 20
    max_step_retries: int = 2
    max_task_seconds: int = 900
    max_concurrent_tasks: int = 2
    tool_timeout_s: int = 60
    sandbox_memory_mb: int = 512
    sandbox_cpu_s: int = 20
    # --- ingestion ------------------------------------------------------------------------
    max_file_mb: int = 512
    chunk_chars: int = 1200
    chunk_overlap: int = 150
    ingest_batch_size: int = 64              # chunks per commit/checkpoint
    ingest_workers: int = 2
    min_chunk_chars: int = 40
    max_reject_rate: float = 0.6             # pause publication when exceeded (after min sample)
    min_sample_for_gate: int = 50
    near_dup_hamming: int = 3
    quarantine_injections: bool = True
    min_free_disk_mb: int = 500
    max_item_seconds: int = 3600
    archive_max_members: int = 5000
    archive_max_ratio: int = 200             # zip-bomb guard (uncompressed / compressed)
    archive_max_total_mb: int = 4096
    keep_raw: bool = True
    # --- web research ---------------------------------------------------------------------
    web_allowlist: list[str] = field(default_factory=list)   # empty = any public host
    web_denylist: list[str] = field(default_factory=lambda: ["localhost"])
    web_allow_private: bool = False          # SSRF guard: block loopback/private addresses
    web_respect_robots: bool = True
    web_user_agent: str = "NexusAI/0.1 (+local research agent)"
    web_rate_limit_s: float = 1.0
    web_max_bytes: int = 5_000_000
    web_max_pages: int = 20
    web_max_depth: int = 1
    search_provider: str = "none"            # none | searxng | brave
    search_url: str = ""                     # searxng base URL
    search_api_key: str = ""
    # --- datasets -------------------------------------------------------------------------
    max_auto_download_mb: int = 500          # larger downloads need explicit approval
    # --- privacy ---------------------------------------------------------------------------
    telemetry: bool = False
    allowed_hosts: list[str] = field(default_factory=lambda: ["localhost", "127.0.0.1", "[::1]"])   # Host headers accepted (DNS-rebinding guard)
    allow_cors_origins: list[str] = field(default_factory=lambda: [
        "http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000"])
    # --- integrations ---------------------------------------------------------------------
    integrations: list[dict] = field(default_factory=list)   # [{name, base_url, auth_env, allow_write}]
    external_tools: list[dict] = field(default_factory=list) # registered CLI utilities
    # --- training -------------------------------------------------------------------------
    llamacpp_dir: str = ""                   # folder with convert_hf_to_gguf.py / llama-quantize

    # ---- derived paths -------------------------------------------------------------------
    def path(self, *parts: str) -> Path:
        p = Path(self.data_dir).joinpath(*parts)
        return p

    def ensure_dirs(self) -> None:
        for d in ("raw", "downloads", "workspace", "reports", "trash", "backups", "logs", "vectors",
                  "web", "training", "extensions/proposed", "extensions/active", "exports", "uploads"):
            self.path(*d.split("/")).mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.path("nexus.db")

    def public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in SECRET_FIELDS:
            d[k] = "***set***" if d.get(k) else ""
        return d


_ENV_PREFIX = "NEXUS_"
_lock = threading.RLock()


def _coerce(value: str, typ: Any, default: Any) -> Any:
    try:
        if isinstance(default, bool):
            return value.strip().lower() in ("1", "true", "yes", "on")
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, list):
            v = value.strip()
            if v.startswith("["):
                return json.loads(v)
            return [x.strip() for x in v.split(",") if x.strip()]
    except ValueError:
        return default
    return value


def load_settings(data_dir: str | None = None) -> Settings:
    _load_dotenv(_repo_root() / ".env")
    s = Settings()
    for f in fields(Settings):
        env = os.environ.get(_ENV_PREFIX + f.name.upper())
        if env is not None:
            setattr(s, f.name, _coerce(env, f.type, getattr(s, f.name)))
    if data_dir:
        s.data_dir = data_dir
    if not s.data_dir:
        s.data_dir = str(_repo_root() / "data")
    if s.execution_mode not in EXECUTION_MODES:
        import logging
        logging.getLogger("nexus.config").warning("Invalid execution_mode %r - using 'approval_required'", s.execution_mode)
        s.execution_mode = "approval_required"
    s.data_dir = str(Path(s.data_dir).expanduser().resolve())
    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    sj = Path(s.data_dir) / "settings.json"
    if sj.is_file():
        try:
            overrides = json.loads(sj.read_text(encoding="utf-8"))
            for k, v in overrides.items():
                if k != "data_dir" and hasattr(s, k):
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
    s.ensure_dirs()
    return s


def save_overrides(s: Settings, changes: dict[str, Any]) -> list[str]:
    """Persist user-made changes. Validates names and a few critical values. Returns changed keys."""
    valid = {f.name for f in fields(Settings)} - {"data_dir"}
    applied: list[str] = []
    with _lock:
        sj = Path(s.data_dir) / "settings.json"
        cur = json.loads(sj.read_text(encoding="utf-8")) if sj.is_file() else {}
        for k, v in changes.items():
            if k not in valid:
                raise ValueError(f"Unknown setting: {k}")
            if k == "execution_mode" and v not in EXECUTION_MODES:
                raise ValueError(f"execution_mode must be one of {EXECUTION_MODES}")
            if k in ("tool_mode",) and v not in ("auto", "native", "prompt"):
                raise ValueError("tool_mode must be auto, native or prompt")
            if v == "***set***":  # UI echoing a masked secret back
                continue
            cur_val = getattr(s, k)
            if isinstance(cur_val, bool) and not isinstance(v, bool):
                raise ValueError(f"{k} must be true/false")
            if isinstance(cur_val, (int, float)) and not isinstance(cur_val, bool) and not isinstance(v, (int, float)):
                raise ValueError(f"{k} must be a number")
            if isinstance(cur_val, list) and not isinstance(v, list):
                raise ValueError(f"{k} must be a list")
            setattr(s, k, v)
            cur[k] = v
            applied.append(k)
        sj.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return applied
