"""Pydantic data models for split, compare and the API boundary."""

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
    PipelineResult,
    RetrievedCandidate,
    normalize_internalization_decision,
)
from .task import TaskInfo, TaskStatusEnum

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
    "PipelineResult",
    "RetrievedCandidate",
    "normalize_internalization_decision",
    "TaskInfo",
    "TaskStatusEnum",
]
