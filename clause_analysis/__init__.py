"""外规内化 · 单条差异分析.

Usage::

    from clause_analysis import ClauseAnalyzer, ClauseInput

    analyzer = ClauseAnalyzer.from_config()
    result = await analyzer.analyze(ClauseInput(term_content=..., term_name="第一条", ...))
    element = result.to_esg()          # one data[] element (algorithm fields)
"""

from .analyzer import ClauseAnalyzer
from .types import (
    NEEDS_INTERNALIZATION,
    NO_INTERNALIZATION,
    ClauseInput,
    ClauseResult,
    CoverageStatus,
    RelatedTerm,
)

__all__ = [
    "ClauseAnalyzer",
    "ClauseInput",
    "ClauseResult",
    "CoverageStatus",
    "NEEDS_INTERNALIZATION",
    "NO_INTERNALIZATION",
    "RelatedTerm",
]
