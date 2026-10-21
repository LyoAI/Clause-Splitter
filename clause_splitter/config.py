"""Global configuration — business knobs only.

Two layers, deliberately kept apart:
- **Business knobs (this module)**: LLM timeouts, review page, compare
  concurrency, report batching, split thresholds — plus the per-article
  chunking contract (CHUNK_LEVEL/SIZE/MIN_TOKENS) and the episode tool budget
  (CONTEXT_SCOPE_MAX / MAX_TOOL_CALLS), passed explicitly to the engine.
- **Engine knobs (chillrag.config)**: retrieval top_k, snippets, embedding,
  tokenizer, MinerU backend. Same-named env vars (SEARCH_TOP_K /
  SNIPPET_MAX_TOKENS / EMBED_MODEL / MINERU_BACKEND …) are read by chillrag;
  they are not redefined here.

``load_dotenv`` runs at import time so heavy dependencies (PySerini's JVM via
``JAVA_HOME``, the OpenAI SDK) see environment variables the moment they are
imported — this import-order guarantee is what makes the BM25 index build work.
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
# Parallel clause episodes during a comparison run.
COMPARE_CONCURRENCY = int(os.getenv("COMPARE_CONCURRENCY", "4"))
# Gap-analysis report: external clauses per batch-summary sub-agent call.
REPORT_BATCH_SIZE = int(os.getenv("REPORT_BATCH_SIZE", "20"))

# ── Per-article chunking contract (passed to chillrag's chunker) ─────
# Directory name under ``doc_cache_dir(name)`` holding MinerU's output.
MINERU_MD_DIRNAME = "md"
# Structure chunking: a ``title`` block with level <= CHUNK_LEVEL opens a new
# chunk; deeper sub-headings stay inside the current chunk. Default 4 = the
# deepest regulation marker level (编=1 章=2 节=3 条=4, as leveled by
# docprep.headings) — every 第X条 becomes its own retrieval unit.
CHUNK_LEVEL = int(os.getenv("CHUNK_LEVEL", "4"))
# Soft token budget per chunk (oversized sections are split, tables kept whole).
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
# Adjacent chunks smaller than this many tokens merge into the previous one
# when the section path matches. 0 = strict section boundaries, no merging.
CHUNK_MIN_TOKENS = int(os.getenv("CHUNK_MIN_TOKENS", "50"))

# ── Index cache ────────────────────────────────────────────────────────
# Derived per-corpus cache root: .cache/<store_name>/{store, chunks, bm25, faiss}.
# ``INDEX_CACHE_DIR`` relocates it (e.g. a shared network volume).
CACHE_DIR = REPO_ROOT / ".cache"
INDEX_CACHE_DIR = os.getenv("INDEX_CACHE_DIR", "")


def doc_cache_dir(store_name: str, root: str | Path | None = None) -> Path:
    """Root for one corpus's derived artifacts: materialized chunk txt,
    the BM25 Lucene index, the faiss vector db, and the serialized store.

    Args:
        store_name: Corpus identity (e.g. "internal" → ``<root>/internal/``).
        root: Override the cache parent; default ``INDEX_CACHE_DIR`` env var,
            then ``CACHE_DIR``.
    """
    base = Path(root) if root else (Path(INDEX_CACHE_DIR) if INDEX_CACHE_DIR else CACHE_DIR)
    return base / store_name


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
    default_model: str = "qwen3-max"
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
