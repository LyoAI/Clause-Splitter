"""Global configuration — service and business knobs.

``load_dotenv`` runs at import time so every module sees the same environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Repo root: ``clause_splitter/config.py`` → two levels up.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Load KEY=VALUE pairs from ``.env`` (never overwrites existing env vars).
load_dotenv(REPO_ROOT / ".env")


# ── LLM ───────────────────────────────────────────────────────────────
# Per-model LLM configs: ``config/llm/<model_name>.yaml``, one section per provider.
LLM_CONFIG_DIR = REPO_ROOT / "config" / "llm"
# Provider implementation: ``openai`` (OpenAI-compatible) or ``pingan`` (signed
# intranet gateway). Names the yaml section to read.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
# Model id — reads ``config/llm/<name>.yaml`` and is sent as the request model.
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "qwen3.8-27b")
# Per-call LLM request timeout (s) and per-episode wall-clock deadline.
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "180"))
# Document profile call — shorter, so it cannot stall the chain.
DOC_PROFILE_TIMEOUT_S = float(os.getenv("DOC_PROFILE_TIMEOUT_S", "90"))
EPISODE_TIMEOUT_S = float(os.getenv("EPISODE_TIMEOUT_S", "600"))
# Agent prompt language: "zh" | "en".
PROMPT_LANG = os.getenv("PROMPT_LANG", "zh")

# ── Compare (review page / episodes / report) ─────────────────────────
# Tool-call budget per clause-comparison episode.
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", "15"))
# Max span (inclusive offsets) of a get_context window.
CONTEXT_SCOPE_MAX = int(os.getenv("CONTEXT_SCOPE_MAX", "8"))
# Top-k internal clauses shown on the review page (display-only pre-retrieval).
# 3 keeps the page as short as the old LLM relevance filter (min 3, adopted kept).
REVIEW_TOP_K = int(os.getenv("REVIEW_TOP_K", "3"))
# Cap one embedded candidate text. Each unique text is stored once (popups and
# Excel export share it); the cap only trims pathological whole-document chunks.
REVIEW_FULL_TEXT_MAX_CHARS = int(os.getenv("REVIEW_FULL_TEXT_MAX_CHARS", "5000"))
# Warn when one review page grows past this size (MB).
REVIEW_SIZE_WARN_MB = int(os.getenv("REVIEW_SIZE_WARN_MB", "50"))
# Parallel clause episodes; keep aligned with the model's concurrency limit.
COMPARE_CONCURRENCY = int(os.getenv("COMPARE_CONCURRENCY", "2"))

# ── External RAG service ───────────────────────────────────────────────
# Which provider implementation to use (see clause_splitter/rag/providers).
RAG_PROVIDER = os.getenv("RAG_PROVIDER", "chillrag")
# Base URL. Empty = retrieval unavailable (analysis fails; parsing falls back local).
RAG_API_BASE = os.getenv("RAG_API_BASE", "")
# Login credentials for providers that issue a session cookie.
RAG_API_USER = os.getenv("RAG_API_USER", "")
RAG_API_PASSWORD = os.getenv("RAG_API_PASSWORD", "")
# Corpus (folder) names: internal regulations and the parse scratch space.
RAG_CORPUS = os.getenv("RAG_CORPUS", "clause_splitter_internal")
RAG_PARSE_CORPUS = os.getenv("RAG_PARSE_CORPUS", "clause_splitter_parse")
# Per-call HTTP timeout, and the upload + ingest polling deadline (seconds).
RAG_TIMEOUT_S = float(os.getenv("RAG_TIMEOUT_S", "30"))
RAG_PARSE_TIMEOUT_S = float(os.getenv("RAG_PARSE_TIMEOUT_S", "1800"))
# External parsing when the remote fails: "local" = extract plain text here,
# "none" = fail the stage.
PARSE_FALLBACK = os.getenv("PARSE_FALLBACK", "local")

# ── Zeus knowledge base (RAG_PROVIDER=zeus) ────────────────────────────
# App credentials sent as x-app-id / x-app-secret; x-index selects the corpus.
ZEUS_APP_ID = os.getenv("ZEUS_APP_ID", "")
ZEUS_APP_SECRET = os.getenv("ZEUS_APP_SECRET", "")
ZEUS_INDEX = os.getenv("ZEUS_INDEX", "")
# Raw recall candidates per kb alias before reranking, and the vendor's max k.
ZEUS_KB_LIMIT = int(os.getenv("ZEUS_KB_LIMIT", "20"))
ZEUS_MAX_K = int(os.getenv("ZEUS_MAX_K", "50"))
# Score floors: recall filter and rerank (0 = omit, let the service default).
ZEUS_RECALL_MIN_SCORE = float(os.getenv("ZEUS_RECALL_MIN_SCORE", "0.1"))
ZEUS_RERANK_MIN_SCORE = float(os.getenv("ZEUS_RERANK_MIN_SCORE", "0.0"))
# Documented gateway concurrency limit for this app.
ZEUS_MAX_CONCURRENCY = int(os.getenv("ZEUS_MAX_CONCURRENCY", "2"))
# Upload is one long request; the normal RAG timeout is too short for big files.
ZEUS_UPLOAD_TIMEOUT_S = float(os.getenv("ZEUS_UPLOAD_TIMEOUT_S", "300"))
# ── Zeus corpus labels ─────────────────────────────────────────────────
# Zeus indexes only documents that carry text_labels; x-app-id / x-index scope
# the whole knowledge base, so labels are what separate the corpora. Every
# ingested document gets a corpus label plus "doc:<stem>", which search reads
# back as the document name (Zeus hits carry no file name).
ZEUS_LABEL_INTERNAL = os.getenv("ZEUS_LABEL_INTERNAL", "internal-regulation")
ZEUS_LABEL_ATTACH = os.getenv("ZEUS_LABEL_ATTACH", "attach-regulation")
# Labels sent in the search filter (comma-separated).
#   unset / all → empty list → text_labels=[] → the whole knowledge base
#   x,y         → only documents carrying one of these labels
_ZEUS_SEARCH_LABELS_RAW = os.getenv("ZEUS_SEARCH_LABELS", "").strip()
ZEUS_SEARCH_LABELS: list[str] = (
    []
    if _ZEUS_SEARCH_LABELS_RAW.lower() in ("", "all")
    else [s.strip() for s in _ZEUS_SEARCH_LABELS_RAW.split(",") if s.strip()]
)
# Uploader recorded on ingest submissions (UM id).
ZEUS_CREATED_BY = os.getenv("ZEUS_CREATED_BY", "LIUKAINAN960")

# ── Retrieval / agent budget ───────────────────────────────────────────
# Top-k internal clauses per search call.
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "10"))
# Snippet token cap for search results handed to the LLM.
SNIPPET_MAX_TOKENS = int(os.getenv("SNIPPET_MAX_TOKENS", "512"))

# ── Per-article chunking contract ──────────────────────────────────────
# A title block at or above this depth opens a new chunk; deeper headings stay
# inside it. 4 = 编/章/节/条, so every article becomes its own retrieval unit.
CHUNK_LEVEL = int(os.getenv("CHUNK_LEVEL", "4"))
# Soft token budget per chunk (oversized sections are split, tables kept whole).
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
# Smaller adjacent chunks merge when they share a section path; 0 = never merge.
CHUNK_MIN_TOKENS = int(os.getenv("CHUNK_MIN_TOKENS", "50"))

# ── Backend integration (push → dispatch → ESG callback) ────────────────
# How pushed regulations reach the chain: immediate | cron | auto.
# auto = optType 0 (manual) pushes run immediately, the rest wait for cron.
CHAIN_DISPATCH = os.getenv("CHAIN_DISPATCH", "auto")
# 5-field crontab admitting scheduled chain jobs (see services.dispatcher).
CHAIN_CRON = os.getenv("CHAIN_CRON", "0 20 * * mon-sat")
# ESG gateway for the result callback; when unset, BACKEND_CALLBACK_URL is used.
ESG_AUTH_END_POINT = os.getenv("ESG_AUTH_END_POINT", "")
ESG_OPEN_END_POINT = os.getenv("ESG_OPEN_END_POINT", "")
ESG_CLIENT_ID = os.getenv("ESG_CLIENT_ID", "")
ESG_CLIENT_SECRET = os.getenv("ESG_CLIENT_SECRET", "")
ESG_CALLBACK_PATH = os.getenv("ESG_CALLBACK_PATH", "/paerm/paermModelReturn")

# ── Push docType → document kind ───────────────────────────────────────
# Backend contract: 1 = 外规 (external), 2 = 内规 (internal), 3 = 附件 (attach).
# Configurable because the mapping is still a verbal agreement.
PUSH_DOCTYPE_EXTERNAL = os.getenv("PUSH_DOCTYPE_EXTERNAL", "1")
PUSH_DOCTYPE_INTERNAL = os.getenv("PUSH_DOCTYPE_INTERNAL", "2")
PUSH_DOCTYPE_ATTACH = os.getenv("PUSH_DOCTYPE_ATTACH", "3")

# 内规/附件跑完「入库+拆条」后是否也回调后端。关掉时只能靠
# GET /regulations/{docId}/result 主动拉取。
_PUSH_INTERNAL_CALLBACK_RAW = os.getenv("PUSH_INTERNAL_CALLBACK", "1").strip().lower()
PUSH_INTERNAL_CALLBACK = _PUSH_INTERNAL_CALLBACK_RAW not in ("0", "false", "no", "off")


# ── Split-pipeline thresholds ──────────────────────────────────────────


@dataclass
class Settings:
    """Split-engine configuration (thresholds, regexes, LLM defaults)."""

    # Document structure analysis — article boundary regexes (priority order).
    # LLM path is preferred; these are fast-path fallbacks.
    article_patterns: list[str] = field(default_factory=lambda: [
        r"第([一二三四五六七八九十百零\d]+)条",
        r"第([一二三四五六七八九十百零\d]+)节",
        r"([（(][一二三四五六七八九十\d]+[）)])",
        r"(Article\s+[IVXLCDM\d]+)",
        r"(\d+\.\d+)",
    ])
    regex_coverage_threshold: float = 0.80

    chapter_patterns: list[tuple[str, str]] = field(default_factory=lambda: [
        (r"第([一二三四五六七八九十百零]+)章\s*[^\n]*", "第X章"),
        (r"(Chapter\s+[IVXLCDM\d]+)[\s:：]", "Chapter X"),
        (r"([一二三四五六七八九十]+)、\s*", "中文顿号编号"),
    ])

    # Complexity estimation
    complex_solo_threshold: float = 6.0   # 复杂度超此值的条款标注为复杂

    complexity_length_tiers: list[tuple[int, float]] = field(default_factory=lambda: [
        (500, 3.0), (200, 2.0), (100, 1.0),
    ])
    complexity_sub_item_tiers: list[tuple[int, float]] = field(default_factory=lambda: [
        (3, 2.0), (0, 1.0),
    ])

    # LLM
    default_model: str = LLM_MODEL_NAME  # needs a matching config/llm/<name>.yaml
    llm_max_input_chars: int = 12000  # max chars sent to LLM in structure analysis

# ── Singleton ──────────────────────────────────────────────────────────

_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global Settings singleton (lazy-init)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
