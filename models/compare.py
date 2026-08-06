"""Compare & summary data models for internal-vs-external regulation matching.

Supports many-to-many matching: multiple external regulations vs multiple
internal policies. Each MatchResult tracks document provenance.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.clause import ClauseSplitResult


# ── Enums ─────────────────────────────────────────────────────────────────

class CoverageStatus(str, Enum):
    """Coverage status for a single external obligation match."""

    FULLY_COVERED = "完全覆盖"
    PARTIALLY_COVERED = "部分覆盖"
    NOT_COVERED = "缺失"


# ── Match result ──────────────────────────────────────────────────────────

class MatchResult(BaseModel):
    """One external obligation matched against internal obligations.

    Document provenance fields (external_document_name, external_article_id,
    matched_internal_document_name) make each match traceable back to its
    source document — essential for many-to-many comparisons.
    """

    model_config = ConfigDict(extra="forbid")

    external_clause_id: str = Field(
        ...,
        description="External obligation index (matches split result index)",
    )
    external_document_name: str = Field(
        default="",
        description="Source external regulation document name",
    )
    external_article_id: str = Field(
        default="",
        description="Original article reference, e.g. '第十四条'",
    )
    external_source_text: str = Field(
        default="",
        description="Full source text of the external obligation — quoted in the report",
    )
    status: CoverageStatus = Field(
        ...,
        description="Coverage status",
    )
    matched_internal_id: str | None = Field(
        default=None,
        description="Matched internal obligation index, null if not covered",
    )
    matched_internal_document_name: str | None = Field(
        default=None,
        description="Source internal policy document name that matched",
    )
    matched_internal_clause_number: str = Field(
        default="",
        description="Clause reference of the matched internal obligation, e.g. '第十四条'",
    )
    matched_internal_source_text: str = Field(
        default="",
        description="Full source text of the matched internal obligation — evidence for the gap analysis report",
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
    """Full comparison result for one or more document pairs.

    Supports many-to-many: external_documents and internal_documents are
    lists of document names. Backward-compat computed properties
    external_document_name / internal_document_name return the first
    element (or empty string).
    """

    model_config = ConfigDict(extra="ignore")  # ignore old single-name fields

    external_documents: list[str] = Field(
        default_factory=list,
        description="List of external regulation document names",
    )
    internal_documents: list[str] = Field(
        default_factory=list,
        description="List of internal policy document names",
    )
    total_external_obligations: int = Field(default=0, ge=0)
    total_internal_obligations: int = Field(
        default=0, ge=0,
        description="Total internal obligations used in comparison",
    )
    matches: list[MatchResult] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_old_format(cls, data: Any) -> Any:
        """Migrate old single-doc-name fields to new list format."""
        if isinstance(data, dict):
            # Old format: external_document_name: str → external_documents: list[str]
            if "external_document_name" in data and not data.get("external_documents"):
                old_name = data.pop("external_document_name", "")
                if old_name:
                    data["external_documents"] = [old_name]
            # Old format: internal_document_name: str → internal_documents: list[str]
            if "internal_document_name" in data and not data.get("internal_documents"):
                old_name = data.pop("internal_document_name", "")
                if old_name:
                    data["internal_documents"] = [old_name]
        return data

    # ── Backward-compat computed properties ───────────────────────────────

    @property
    def external_document_name(self) -> str:
        """Backward compat: first external document name."""
        return self.external_documents[0] if self.external_documents else ""

    @property
    def internal_document_name(self) -> str:
        """Backward compat: first internal document name."""
        return self.internal_documents[0] if self.internal_documents else ""

    # ── Statistics computed properties ────────────────────────────────────

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
        """Return a dict suitable for LLM consumption in summary report."""
        return {
            "external_documents": self.external_documents,
            "internal_documents": self.internal_documents,
            "total_external_obligations": self.total_external_obligations,
            "total_internal_obligations": self.total_internal_obligations,
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
