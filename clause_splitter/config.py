"""Global configuration — service and business knobs.

Retrieval itself lives in the external RAG service (``clause_splitter/rag``);
this module holds the client address, timeouts, and the chunking / agent
contracts that stay in this repository. ``load_dotenv`` runs at import time so
every module sees the same environment.
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
# Per-model LLM configs: ``config/llm/<model_name>.yaml`` with one section per
# provider type (``openai:`` / ``pingan:``); ``LLM(model_name, provider_type)``
# reads the section matching ``provider_type``.
LLM_CONFIG_DIR = REPO_ROOT / "config" / "llm"
# Per-call LLM request timeout (s) and per-episode wall-clock deadline.
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "180"))
# Document summary / keyword call — shorter so a slow profile can't stall the chain.
DOC_PROFILE_TIMEOUT_S = float(os.getenv("DOC_PROFILE_TIMEOUT_S", "90"))
EPISODE_TIMEOUT_S = float(os.getenv("EPISODE_TIMEOUT_S", "600"))
# Agent prompt language: "zh" | "en".
PROMPT_LANG = os.getenv("PROMPT_LANG", "zh")

# ── Compare (review page / episodes / report) ─────────────────────────
# Tool-call budget per clause-comparison episode.
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", "15"))
# Max span (inclusive offsets) of a get_context window.
CONTEXT_SCOPE_MAX = int(os.getenv("CONTEXT_SCOPE_MAX", "8"))
# Top-k internal clauses surfaced on the HTML review page for each external
# clause (deterministic batch pre-retrieval, display-only).
REVIEW_TOP_K = int(os.getenv("REVIEW_TOP_K", "10"))
# LLM relevance filter on the review page: judge whether each top-k candidate has
# any relation to the external clause and drop the unrelated ones (display-only;
# compare_result.json keeps the raw candidates). 1 = on, 0 = off.
REVIEW_FILTER_RELEVANCE = int(os.getenv("REVIEW_FILTER_RELEVANCE", "1"))
# Truncation (chars) for the external clause / per candidate when judging relevance.
REVIEW_FILTER_EXTERNAL_MAX_CHARS = int(os.getenv("REVIEW_FILTER_EXTERNAL_MAX_CHARS", "500"))
REVIEW_FILTER_CANDIDATE_MAX_CHARS = int(os.getenv("REVIEW_FILTER_CANDIDATE_MAX_CHARS", "300"))
# Minimum candidates kept on the review page after the relevance filter: top-up by
# score regardless of relevance, so a clause never shows an empty recall list merely
# because the strict filter dropped everything.
REVIEW_MIN_CANDIDATES = int(os.getenv("REVIEW_MIN_CANDIDATES", "3"))
# Parallel clause episodes during a comparison run. Default 2 = the intranet
# qwen3.8-27b serves exactly 2 concurrent slots — raise only if ops does.
COMPARE_CONCURRENCY = int(os.getenv("COMPARE_CONCURRENCY", "2"))
# Gap-analysis report: external clauses per batch-summary sub-agent call.
REPORT_BATCH_SIZE = int(os.getenv("REPORT_BATCH_SIZE", "20"))

# ── External RAG service ───────────────────────────────────────────────
# Base URL of the RAG HTTP service. Empty = retrieval unavailable; analysis
# fails with a readable "未配置" message and external parsing falls back to
# local text extraction.
RAG_API_BASE = os.getenv("RAG_API_BASE", "")
# Sent as ``Authorization: Bearer <key>`` when non-empty.
RAG_API_KEY = os.getenv("RAG_API_KEY", "")
# Per-call timeout for search / inspect endpoints (seconds).
RAG_TIMEOUT_S = float(os.getenv("RAG_TIMEOUT_S", "30"))
# Parse / ingest uploads may run OCR server-side — allow a long deadline.
RAG_PARSE_TIMEOUT_S = float(os.getenv("RAG_PARSE_TIMEOUT_S", "300"))
# External parsing when the remote fails: "local" = extract plain text here,
# "none" = fail the stage.
PARSE_FALLBACK = os.getenv("PARSE_FALLBACK", "local")

# ── Retrieval / agent budget ───────────────────────────────────────────
# Top-k internal clauses per search call.
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "10"))
# Snippet token cap for search results handed to the LLM.
SNIPPET_MAX_TOKENS = int(os.getenv("SNIPPET_MAX_TOKENS", "512"))

# ── Per-article chunking contract ──────────────────────────────────────
# Structure chunking: a ``title`` block with level <= CHUNK_LEVEL opens a new
# chunk; deeper sub-headings stay inside the current chunk. Default 4 = the
# deepest regulation marker level (编=1 章=2 节=3 条=4, as leveled by
# compare.headings) — every 第X条 becomes its own retrieval unit.
CHUNK_LEVEL = int(os.getenv("CHUNK_LEVEL", "4"))
# Soft token budget per chunk (oversized sections are split, tables kept whole).
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
# Adjacent chunks smaller than this many tokens merge into the previous one
# when the section path matches. 0 = strict section boundaries, no merging.
CHUNK_MIN_TOKENS = int(os.getenv("CHUNK_MIN_TOKENS", "50"))

# ── Backend integration (push → dispatch → ESG callback) ────────────────
# How pushed regulations reach the chain: immediate | cron | auto.
# auto = OptType 0 (manual) pushes run immediately, the rest wait for cron.
CHAIN_DISPATCH = os.getenv("CHAIN_DISPATCH", "auto")
# 5-field crontab admitting scheduled chain jobs (see services.dispatcher).
CHAIN_CRON = os.getenv("CHAIN_CRON", "0 20 * * mon-sat")
# ESG open-platform gateway for the result callback. Empty ESG_OPEN_END_POINT
# disables the gateway; BACKEND_CALLBACK_URL then acts as a plain dev fallback.
ESG_AUTH_END_POINT = os.getenv("ESG_AUTH_END_POINT", "")
ESG_OPEN_END_POINT = os.getenv("ESG_OPEN_END_POINT", "")
ESG_CLIENT_ID = os.getenv("ESG_CLIENT_ID", "")
ESG_CLIENT_SECRET = os.getenv("ESG_CLIENT_SECRET", "")
ESG_CALLBACK_PATH = os.getenv("ESG_CALLBACK_PATH", "/paerm/paermModelReturn")


# ── Split-pipeline thresholds (legacy Settings) ────────────────────────
# All magic numbers / regexes for the clause-decomposition pipeline live here
# so they can be tuned without touching pipeline code.


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

    # Batch planning
    max_batch_chars: int = 30000          # 一次 batch 最多 30K 字符
    max_batch_obligations: int = 100      # 或 100 条预估义务
    complex_solo_threshold: float = 6.0   # 复杂度超此值的条款单独成批

    # Complexity estimation
    complexity_length_tiers: list[tuple[int, float]] = field(default_factory=lambda: [
        (500, 3.0), (200, 2.0), (100, 1.0),
    ])
    complexity_sub_item_tiers: list[tuple[int, float]] = field(default_factory=lambda: [
        (3, 2.0), (0, 1.0),
    ])

    # ReAct decomposition
    max_react_iterations: int = 5

    # LLM
    default_model: str = "qwen3.8-27b"  # intranet model; needs config/llm/qwen3.8-27b.yaml
    llm_max_input_chars: int = 12000  # max chars sent to LLM in structure analysis

    # Obligation rule keyword mapping
    obligation_shall_words: set[str] = field(default_factory=lambda: {"应当", "必须", "需", "须", "要"})
    obligation_prohibit_words: set[str] = field(default_factory=lambda: {"禁止", "不得", "严禁"})

# ── Singleton ──────────────────────────────────────────────────────────

_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global Settings singleton (lazy-init)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
