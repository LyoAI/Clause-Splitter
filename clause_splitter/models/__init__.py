"""Pydantic data models for split and compare engines."""

from .clause import (
    ArticleInfo,
    ClauseObligation,
    ClauseSplitResult,
    ComplexityLabel,
    DocumentStructure,
    ObligationRule,
    StructureLevel,
)
from .compare import (
    CompareResult,
    CoverageStatus,
    MatchResult,
    RetrievedCandidate,
    normalize_internalization_decision,
)

__all__ = [
    "ArticleInfo",
    "ClauseObligation",
    "ClauseSplitResult",
    "ComplexityLabel",
    "DocumentStructure",
    "ObligationRule",
    "StructureLevel",
    "CompareResult",
    "CoverageStatus",
    "MatchResult",
    "RetrievedCandidate",
    "normalize_internalization_decision",
]
