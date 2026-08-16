"""Global configuration — the single place for all tunable knobs.

Merge of the two reference configs:
- FinIndicator-Bench ``config.py`` module-level constants (LLM, searcher,
  embedding, index cache) for the agent / compare side.
- the legacy ``config/settings.py`` ``Settings`` dataclass (split-side
  thresholds) for the clause-decomposition pipeline.

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
# External OpenAI-compatible endpoint (DashScope compatible-mode by default).
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3-max")
LLM_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
# Per-model LLM configs: ``config/llm/<model_name>.yaml`` with one section per
# provider type (``openai:`` / ``pingan:``); ``LLM(model_name, provider_type)``
# reads the section matching ``provider_type``.
LLM_CONFIG_DIR = REPO_ROOT / "config" / "llm"
# Per-call LLM request timeout (s) and per-episode wall-clock deadline.
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "180"))
EPISODE_TIMEOUT_S = float(os.getenv("EPISODE_TIMEOUT_S", "600"))
# Agent prompt language: "zh" | "en".
PROMPT_LANG = os.getenv("PROMPT_LANG", "zh")

# ── Agent tool layer ───────────────────────────────────────────────────
# Which retrieval backends to bind as tools: "grep" | "bm25" | "faiss" | "hybrid".
# "hybrid" binds all three so the LLM picks a search tool per turn.
SEARCHER_TYPE = os.getenv("SEARCHER_TYPE", "hybrid")
# Fixed number of hits every search tool returns.
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "10"))
# Top-k internal clauses surfaced on the HTML review page for each external
# clause (deterministic batch pre-retrieval, display-only).
REVIEW_TOP_K = int(os.getenv("REVIEW_TOP_K", "10"))
# Max tokens per search snippet exposed to the LLM (truncated once, in the registry).
SNIPPET_MAX_TOKENS = int(os.getenv("SNIPPET_MAX_TOKENS", "512"))
# Max span (inclusive offsets) of a get_context window.
CONTEXT_SCOPE_MAX = int(os.getenv("CONTEXT_SCOPE_MAX", "8"))
# Tool-call budget per clause-comparison episode.
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", "15"))
# Parallel clause episodes during a comparison run.
COMPARE_CONCURRENCY = int(os.getenv("COMPARE_CONCURRENCY", "4"))
# Gap-analysis report: external clauses per batch-summary sub-agent call.
REPORT_BATCH_SIZE = int(os.getenv("REPORT_BATCH_SIZE", "20"))

# ── Embedding (dense faiss index) ──────────────────────────────────────
# Local models root (modelscope-downloaded models), e.g. ~/Models/... .
MODELS_DIR = Path(os.getenv("MODELS_DIR", str(Path.home() / "Models")))
# Local sentence-transformer model vectorizing clause chunks. Point EMBED_MODEL
# at another local model (e.g. the bge-m3 copy) to switch dense retrieval.
EMBED_MODEL = os.getenv("EMBED_MODEL", str(MODELS_DIR / "Qwen3-Embedding-0.6B"))
# Chunks per encode call.
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))
# Sentence-transformers device. Default "cpu" sidesteps the macOS MPS-backend
# OOM during long batch encodes; set "cuda" (or "auto") on GPU servers.
EMBED_DEVICE = os.getenv("EMBED_DEVICE", "cpu")

# ── Parsing & chunking (MinerU) ───────────────────────────────────────
# MinerU local backend ("pipeline" | "vlm" | "hybrid"); the CLI picks the device.
MINERU_BACKEND = os.getenv("MINERU_BACKEND", "pipeline")
# Directory name under ``doc_cache_dir(name)`` holding MinerU's output.
MINERU_MD_DIRNAME = "md"
# Structure chunking: a ``title`` block with level <= CHUNK_LEVEL opens a new
# chunk; deeper sub-headings stay inside the current chunk.
# Default 4 = the deepest regulation marker level (编=1, 章=2, 节=3, 条=4) as
# assigned by ``headings.infer_regulation_headings`` — so every 第X条 opens its
# own retrieval unit (unified per-article granularity), with chapters kept as
# ``meta.section`` context. Non-regulation documents (no markers) still use
# MinerU's own heading levels. Set 3 (or lower) to go back to coarse
# chapter-level chunks.
CHUNK_LEVEL = int(os.getenv("CHUNK_LEVEL", "4"))
# Soft token budget per chunk (oversized sections are split, tables kept whole).
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
# Adjacent chunks smaller than this many tokens merge into the previous one
# when the section path matches. 0 = strict section boundaries, no merging.
CHUNK_MIN_TOKENS = int(os.getenv("CHUNK_MIN_TOKENS", "50"))

# ── Index cache ────────────────────────────────────────────────────────
# Derived per-corpus cache root: .cache/<store_name>/{store, chunks, bm25, faiss}.
# ``INDEX_CACHE_DIR`` relocates it (e.g. a shared network volume); ``--index-dir``
# on run_index.py / run_compare.py wins over this env var.
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
    """Split-pipeline configuration (thresholds, regexes, export template)."""

    # File handling
    allowed_extensions: set[str] = field(default_factory=lambda: {".pdf", ".docx", ".doc", ".txt", ".xlsx", ".xls"})
    max_file_size_bytes: int = 500 * 1024 * 1024  # 500 MB（超大 Excel 内规库）
    max_chars_per_document: int = 200_000
    min_chars_for_analysis: int = 10

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
    max_self_correct_retries: int = 3

    # Excel clause parsing
    excel_max_sample_rows: int = 30          # rows sent to LLM for schema detection
    excel_max_react_iterations: int = 3      # ReAct retries for Excel schema detection
    max_excel_rows_per_document: int = 20000  # safety cap for very large Excel files

    # Compare (legacy field; the new compare agent picks top_k itself)
    compare_candidate_k: int = 12

    # LLM
    default_model: str = "qwen3-max"
    default_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_temperature: float = 0.1
    llm_max_workers: int = 4
    llm_max_retries: int = 3
    llm_max_input_chars: int = 12000  # max chars sent to LLM in structure analysis

    # Obligation rule keyword mapping
    obligation_shall_words: set[str] = field(default_factory=lambda: {"应当", "必须", "需", "须", "要"})
    obligation_prohibit_words: set[str] = field(default_factory=lambda: {"禁止", "不得", "严禁"})

    # Excel export
    template_path: str = ""
    data_start_row: int = 5            # 1-4 are header rows
    header_rows: int = 4
    default_row_height: float = 30.0
    default_font_name: str = "Baskerville"
    default_font_size: int = 12

    # Progress milestones (percent) — the API polls these for task progress
    progress_milestones: dict[str, float] = field(default_factory=lambda: {
        "parse_complete": 5.0,
        "structure_complete": 15.0,
        "batches_planned": 20.0,
        "decompose_start": 20.0,
        "decompose_end": 80.0,
        "validate_complete": 90.0,
        "export_complete": 100.0,
    })

    def __post_init__(self) -> None:
        if not self.template_path:
            self.template_path = str(REPO_ROOT / "outputs" / "output_excel_template.xlsx")


# ── Singleton ──────────────────────────────────────────────────────────

_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global Settings singleton (lazy-init)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
