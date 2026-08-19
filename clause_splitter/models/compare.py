"""Compare & summary data models for internal-vs-external regulation matching.

Supports many-to-many matching: multiple external regulations vs multiple
internal policies. Each MatchResult tracks document provenance.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clause_splitter.models.clause import ClauseSplitResult


# ── Enums ─────────────────────────────────────────────────────────────────

class CoverageStatus(str, Enum):
    """Coverage status for a single external obligation match."""

    FULLY_COVERED = "完全覆盖"
    PARTIALLY_COVERED = "部分覆盖"
    NOT_COVERED = "缺失"


#: 内化判定的唯二取值:需要内化 / 无需内化。
NEEDS_INTERNALIZATION = "需要内化"
NO_INTERNALIZATION = "无需内化"


def normalize_internalization_decision(decision: str) -> str:
    """内化判定两值化:「预防性内化」并入「需要内化」。

    历史上内化判定有三值(需要内化/预防性内化/无需内化);为简化页面与报告,
    收敛为二值。指导性/金融科技特殊条款的「预防性内化」按保守口径归入
    「需要内化」——仍在复核/整改范围内,缘由保留在 internalization_reason。
    空值/未知值原样返回(渲染层回「待判定」)。
    """
    d = (decision or "").strip()
    if d == "预防性内化":
        return NEEDS_INTERNALIZATION
    return d


# ── Retrieved candidate (HTML 复核展示) ──────────────────────────────────

class RetrievedCandidate(BaseModel):
    """One top-k internal clause retrieved for an external clause.

    Display-only evidence for the 作业人员 review page: the deterministic batch
    pre-retrieval that produced it never changes the episode semantics — it just
    shows what was recalled so the operator can judge relevance / trust.
    """

    chunk_id: str = Field(..., description="Internal chunk id (doc_id#order)")
    doc_name: str = Field(default="", description="Internal regulation name (doc_id)")
    clause_label: str = Field(
        default="",
        description="Section leaf of the chunk — e.g. '第一条' or '第一章　总则'",
    )
    backend: str = Field(
        default="", description="Which searcher(s) recalled it: grep|bm25|faiss (comma-joined)",
    )
    score: float | None = Field(default=None, description="Best score across searchers (nullable)")
    query: str = Field(default="", description="The external clause text used as the query")
    snippet: str = Field(default="", description="Truncated, keyword-highlighted snippet")
    full_text: str = Field(default="", description="Full chunk text (expandable in the HTML)")
    adopted: bool = Field(
        default=False,
        description="True when the episode's grounding adopted this chunk as the match",
    )


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
    matched_internal_clause_ids: list[str] = Field(
        default_factory=list,
        description="All internal clause ids grounded as adopted by the episode "
                    "(head id in matched_internal_id; this list keeps the full set for "
                    "multi-clause adoption)",
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
    clause_type: str = Field(
        default="",
        description="External clause type per the internalization framework: "
                    "义务性条款 / 指导性条款 / 宣示性条款 (empty = not yet judged)",
    )
    internalization_decision: str = Field(
        default="",
        description="Whether this external clause needs to be internalized: "
                    "需要内化 / 无需内化 (empty = 待判定;旧数据『预防性内化』"
                    "经 normalize_internalization_decision 归并为『需要内化』)",
    )
    internalization_reason: str = Field(
        default="",
        description="Rationale for the internalization decision (clause type + "
                    "applicability + five-dimension assessment / special cases)",
    )
    difference_analysis: str = Field(
        default="",
        description="For PARTIALLY_COVERED only: concrete differences between the "
                    "external requirement and the internal provision (missing "
                    "sub-requirements / looser constraints / scope mismatch / vagueness)",
    )
    retrieved_candidates: list[RetrievedCandidate] = Field(
        default_factory=list,
        description="Top-k internal clauses batch-pre-retrieved for this external "
                    "clause (display-only review evidence, not part of the episode)",
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
    def internalizable_count(self) -> int:
        """覆盖率分母:需要内化处理的条款数(需要内化 + 待判定)。

        "无需内化"的条款不进入覆盖比对,不计入分母;前置判定失败(字段为空)
        按需内化处理保守计入,避免漏掉覆盖统计。
        """
        return sum(1 for m in self.matches if (m.internalization_decision or "").strip() != "无需内化")

    @property
    def no_internalization_count(self) -> int:
        """无需内化的条款数(宣示性/程序性等),不参与覆盖率统计。"""
        return sum(1 for m in self.matches if (m.internalization_decision or "").strip() == "无需内化")

    @property
    def internalizable_missing_count(self) -> int:
        """需内化处理但缺失的条款数(排除"无需内化",真实制度缺口)。"""
        return sum(
            1 for m in self.matches
            if m.status == CoverageStatus.NOT_COVERED
            and (m.internalization_decision or "").strip() != "无需内化"
        )

    @property
    def coverage_rate(self) -> float:
        """覆盖率 = (完全覆盖 + 部分覆盖) / 需内化处理条款数。

        只统计需要内化的外规条款;"无需内化"的条款排除在外。
        无需要内化的条款时视为无缺口(1.0)。
        """
        total = self.internalizable_count
        if total == 0:
            return 1.0
        return (self.fully_covered_count + self.partially_covered_count) / total

    def to_summary_dict(self) -> dict:
        """Return a dict suitable for LLM consumption in summary report."""
        return {
            "external_documents": self.external_documents,
            "internal_documents": self.internal_documents,
            "total_external_obligations": self.total_external_obligations,
            "total_internal_obligations": self.total_internal_obligations,
            "details": [m.model_dump(exclude={"retrieved_candidates"}) for m in self.matches],
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
