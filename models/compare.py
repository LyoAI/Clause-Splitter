"""Compare & summary data models for internal-vs-external regulation matching."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from models.clause import ClauseSplitResult


# ── Enums ─────────────────────────────────────────────────────────────────

class CoverageStatus(str, Enum):
    """Coverage status for a single external obligation match."""

    FULLY_COVERED = "完全覆盖"
    PARTIALLY_COVERED = "部分覆盖"
    NOT_COVERED = "缺失"


# ── Match result ──────────────────────────────────────────────────────────

class MatchResult(BaseModel):
    """One external obligation matched against internal obligations."""

    model_config = ConfigDict(extra="forbid")

    external_clause_id: str = Field(
        ...,
        description="External obligation index (matches split result index)",
    )
    status: CoverageStatus = Field(
        ...,
        description="Coverage status",
    )
    matched_internal_id: str | None = Field(
        default=None,
        description="Matched internal obligation index, null if not covered",
    )
    analysis: str = Field(
        ...,
        min_length=1,
        description="Brief explanation of match or gap",
    )
    remedy_suggestion: str = Field(
        default="",
        description="Suggested fix for gap or partial coverage",
    )


# ── Compare result ────────────────────────────────────────────────────────

class CompareResult(BaseModel):
    """Full comparison result for one document pair."""

    model_config = ConfigDict(extra="forbid")

    external_document_name: str = Field(default="")
    internal_document_name: str = Field(default="")
    total_external_obligations: int = Field(default=0, ge=0)
    matches: list[MatchResult] = Field(default_factory=list)

    @property
    def fully_covered_count(self) -> int:
        return sum(1 for m in self.matches if m.status == CoverageStatus.FULLY_COVERED)

    @property
    def partially_covered_count(self) -> int:
        return sum(1 for m in self.matches if m.status == CoverageStatus.PARTIALLY_COVERED)

    @property
    def not_covered_count(self) -> int:
        return sum(1 for m in self.matches if m.status == CoverageStatus.NOT_COVERED)

    @property
    def coverage_rate(self) -> float:
        total = len(self.matches)
        if total == 0:
            return 0.0
        return self.fully_covered_count / total

    def to_summary_dict(self) -> dict:
        """Return a dict suitable for LLM consumption."""
        return {
            "fully_covered": self.fully_covered_count,
            "partially_covered": self.partially_covered_count,
            "not_covered": self.not_covered_count,
            "coverage_rate": f"{self.coverage_rate:.1%}",
            "details": [m.model_dump() for m in self.matches],
        }


# ── Pipeline result ───────────────────────────────────────────────────────

class PipelineResult(BaseModel):
    """Complete pipeline result: split + compare + summary."""

    model_config = ConfigDict(extra="forbid")

    document_name: str = Field(default="")
    split_result: ClauseSplitResult | None = Field(default=None)
    compare_result: CompareResult | None = Field(default=None)
    summary_report: str = Field(default="", description="Markdown gap analysis report")
    summary_report_path: str = Field(default="", description="Path to saved report .md file")
