"""Centralized app settings — all tunable parameters.

All magic numbers, regex patterns, and thresholds live here so they can be
adjusted without touching agent/node code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Settings:
    """Application-wide configuration."""

    # ── File handling ───────────────────────────────────────────────────
    allowed_extensions: set[str] = field(default_factory=lambda: {".pdf", ".docx", ".doc", ".txt", ".xlsx", ".xls"})
    max_file_size_bytes: int = 50 * 1024 * 1024  # 50 MB
    max_chars_per_document: int = 200_000
    min_chars_for_analysis: int = 10

    # ── Document structure analysis ─────────────────────────────────────
    # Article boundary regexes — ordered by priority. The LLM path is
    # preferred; these are fast-path fallbacks.
    article_patterns: list[str] = field(default_factory=lambda: [
        r"第([一二三四五六七八九十百零\d]+)条",
        r"第([一二三四五六七八九十百零\d]+)节",
        r"([（(][一二三四五六七八九十\d]+[）)])",
        r"(Article\s+[IVXLCDM\d]+)",
        r"(\d+\.\d+)",
    ])
    # Minimum regex coverage ratio before falling back to LLM segmentation
    regex_coverage_threshold: float = 0.80

    chapter_patterns: list[tuple[str, str]] = field(default_factory=lambda: [
        (r"第([一二三四五六七八九十百零]+)章\s*[^\n]*", "第X章"),
        (r"(Chapter\s+[IVXLCDM\d]+)[\s:：]", "Chapter X"),
        (r"([一二三四五六七八九十]+)、\s*", "中文顿号编号"),
    ])

    # ── Batch planning ──────────────────────────────────────────────────
    max_batch_chars: int = 30000          # 一次 batch 最多 30K 字符
    max_batch_obligations: int = 100      # 或 100 条预估义务
    complex_solo_threshold: float = 6.0   # only truly complex articles get solo batches

    # ── Complexity estimation ───────────────────────────────────────────
    complexity_length_tiers: list[tuple[int, float]] = field(default_factory=lambda: [
        (500, 3.0), (200, 2.0), (100, 1.0),
    ])
    complexity_sub_item_tiers: list[tuple[int, float]] = field(default_factory=lambda: [
        (3, 2.0), (0, 1.0),
    ])

    # ── ReAct decomposition ─────────────────────────────────────────────
    max_react_iterations: int = 5
    max_self_correct_retries: int = 3

    # ── Excel clause parsing ────────────────────────────────────────────
    excel_max_sample_rows: int = 30          # rows sent to LLM for schema detection
    excel_max_react_iterations: int = 3      # ReAct retries for Excel schema detection
    max_excel_rows_per_document: int = 5000  # safety cap for very large Excel files

    # ── Compare Agent (BM25 + ReAct) ────────────────────────────────────
    compare_batch_size: int = 15             # ext obligations per ReAct batch
    compare_bm25_top_k: int = 20             # default BM25 search results per query
    compare_max_react_iterations: int = 5    # max ReAct loops per batch
    compare_bm25_fallback_threshold: int = 30  # int obls below this → no BM25, full send

    # ── LLM ─────────────────────────────────────────────────────────────
    default_model: str = "qwen3-max"
    default_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_temperature: float = 0.1
    llm_max_workers: int = 4
    llm_max_retries: int = 3
    llm_max_input_chars: int = 12000  # max chars sent to LLM in structure analysis

    # ── Obligation rule keyword mapping ─────────────────────────────────
    obligation_shall_words: set[str] = field(default_factory=lambda: {"应当", "必须", "需", "须", "要"})
    obligation_prohibit_words: set[str] = field(default_factory=lambda: {"禁止", "不得", "严禁"})

    # ── Excel export ────────────────────────────────────────────────────
    template_path: str = ""
    data_start_row: int = 5            # 1-4 are header rows
    header_rows: int = 4
    default_row_height: float = 30.0
    default_font_name: str = "Baskerville"
    default_font_size: int = 12

    # ── Progress milestones ─────────────────────────────────────────────
    progress_milestones: dict[str, float] = field(default_factory=lambda: {
        "parse_complete": 5.0,
        "structure_complete": 15.0,
        "batches_planned": 20.0,
        "decompose_start": 20.0,
        "decompose_end": 80.0,
        "validate_complete": 90.0,
        "export_complete": 100.0,
    })

    # ── Paths ───────────────────────────────────────────────────────────
    project_root: str = ""

    def __post_init__(self) -> None:
        if not self.project_root:
            self.project_root = str(Path(__file__).parent.parent)
        if not self.template_path:
            self.template_path = str(
                Path(self.project_root) / "outputs" / "output_excel_template.xlsx"
            )


# ── Singleton ──────────────────────────────────────────────────────────────

_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global Settings singleton (lazy-init)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
