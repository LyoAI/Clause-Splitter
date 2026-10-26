"""Pydantic models — split-side obligations and compare-side matching.

Chinese Excel headers are serialization aliases: serialize with ``by_alias=True``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ObligationRule(str, Enum):
    """Obligation rule — 应当 (shall) or 禁止 (prohibit)."""

    SHALL = "应当"
    PROHIBIT = "禁止"


class StructureLevel(str, Enum):
    """Document hierarchy levels."""

    CHAPTER = "章"
    SECTION = "节"
    ARTICLE = "条"
    PARAGRAPH = "款"
    ITEM = "项"


class ComplexityLabel(str, Enum):
    """Clause complexity classification."""

    SIMPLE = "simple"
    COMPLEX = "complex"


class ClauseObligation(BaseModel):
    """Single atomic obligation — one verb = one record."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    index: int = Field(
        default=0,
        serialization_alias="序号",
        description="A — row index",
    )
    trigger_condition: str = Field(
        default="/",
        serialization_alias="触发义务条件",
        description="B — time/place/scenario/precondition that triggers this obligation",
    )
    trigger_sub_condition: str = Field(
        default="/",
        serialization_alias="触发义务子条件",
        description="C — sub-condition (if any)",
    )
    subject: str = Field(
        default="/",
        serialization_alias="义务主体",
        description="D — entity or role that performs the action (most likely one)",
    )
    object: str = Field(
        default="/",
        serialization_alias="义务客体",
        description="E — target of the action (entity/person/matter)",
    )
    obligation_rule: ObligationRule = Field(
        default=ObligationRule.SHALL,
        serialization_alias="义务规则",
        description="F — '应当' or '禁止'",
    )
    action_verb: str = Field(
        default="/",
        serialization_alias="义务动作",
        description="G — single predicate verb, e.g. '负责' '建立' '承担'",
    )
    action_target: str = Field(
        default="/",
        serialization_alias="义务事项",
        description="H — specific business content the verb points to",
    )
    method: str = Field(
        default="/",
        serialization_alias="实现义务方式",
        description="I — how the obligation is fulfilled",
    )
    document_name: str = Field(
        default="",
        serialization_alias="文件名称",
        description="J — source regulation/document name",
    )
    clause_number: str = Field(
        default="",
        serialization_alias="条款编号",
        description="K — clause reference, e.g. '第十四条'",
    )
    source_text: str = Field(
        default="",
        serialization_alias="引用原文",
        description="L — shortest complete original sentence",
    )
    ambiguity_note: str = Field(
        default="无歧义",
        serialization_alias="歧义说明",
        description="M — '无歧义' or specific ambiguity explanation",
    )
    ambiguity_detail: str = Field(
        default="",
        serialization_alias="歧义详情",
        description="Detailed ambiguity notes; empty when unambiguous",
    )
    should_split: str = Field(
        default="是",
        serialization_alias="是否拆分",
        description="N — '是'=需拆分提取义务, '否'=定义/列举类条款不拆分",
    )

    @field_validator("trigger_condition", "trigger_sub_condition", "object", "method")
    @classmethod
    def normalize_slash(cls, v: str) -> str:
        """Normalize empty / placeholder values to '/'."""
        stripped = v.strip()
        if stripped in ("", "无", "None", "N/A", "n/a"):
            return "/"
        return stripped

    @field_validator("ambiguity_note")
    @classmethod
    def validate_ambiguity(cls, v: str) -> str:
        """Default to '无歧义' when empty."""
        stripped = v.strip()
        if not stripped or stripped in ("无", "None", ""):
            return "无歧义"
        return stripped


class ClauseSplitResult(BaseModel):
    """Complete split result for one document."""

    model_config = ConfigDict(extra="forbid")

    document_name: str = Field(description="Source file name")
    total_articles: int = Field(default=0, ge=0)
    total_obligations: int = Field(default=0, ge=0)
    obligations: list[ClauseObligation] = Field(default_factory=list)
    processing_notes: list[str] = Field(default_factory=list)
    processing_time_seconds: float = Field(default=0.0, ge=0.0)
    model_used: str = Field(default="")
    excel_path: str = Field(default="", description="Exported Excel file path")


class ArticleInfo(BaseModel):
    """Structural info for one article."""

    model_config = ConfigDict(extra="forbid")

    article_id: str = Field(..., min_length=1, description="e.g. '第十四条'")
    level: StructureLevel = Field(default=StructureLevel.ARTICLE)
    full_text: str = Field(..., min_length=1)
    parent_section: str | None = Field(default=None)
    complexity_score: float = Field(default=1.0, ge=0.0, le=10.0)
    complexity_label: ComplexityLabel = Field(default=ComplexityLabel.SIMPLE)
    estimated_obligation_count: int = Field(default=1, ge=0)


class DocumentStructure(BaseModel):
    """Document structure analysis result."""

    model_config = ConfigDict(extra="forbid")

    document_title: str = Field(default="")
    articles: list[ArticleInfo] = Field(default_factory=list)
    total_character_count: int = Field(default=0, ge=0)
    structure_type: str = Field(
        default="standard",
        description="standard | chapter_based | flat | mixed",
    )


class CoverageStatus(str, Enum):
    """Coverage status for a single external obligation match."""

    FULLY_COVERED = "完全覆盖"
    PARTIALLY_COVERED = "部分覆盖"
    NOT_COVERED = "缺失"


#: 内化判定的唯二取值:需要内化 / 无需内化。
NEEDS_INTERNALIZATION = "需要内化"
NO_INTERNALIZATION = "无需内化"


def normalize_internalization_decision(decision: str) -> str:
    """两值化:「预防性内化」保守归入「需要内化」;空值/未知值原样返回。"""
    d = (decision or "").strip()
    if d == "预防性内化":
        return NEEDS_INTERNALIZATION
    return d


class RetrievedCandidate(BaseModel):
    """One pre-retrieved internal clause — review-page evidence only."""

    chunk_id: str = Field(..., description="Internal chunk id (doc_id#order)")
    doc_name: str = Field(default="", description="Internal regulation name (doc_id)")
    clause_label: str = Field(
        default="",
        description="Section leaf of the chunk — e.g. '第一条' or '第一章　总则'",
    )
    backend: str = Field(
        default="", description="What recalled it: the RAG service name or \"episode\".",
    )
    score: float | None = Field(default=None, description="Best score across searchers (nullable)")
    query: str = Field(default="", description="The external clause text used as the query")
    snippet: str = Field(default="", description="Truncated, keyword-highlighted snippet")
    full_text: str = Field(default="", description="Full chunk text (expandable in the HTML)")
    adopted: bool = Field(
        default=False,
        description="True when the episode's grounding adopted this chunk as the match",
    )


class MatchResult(BaseModel):
    """One external clause matched against internal clauses, with provenance."""

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
    core_regulatory_requirements: str = Field(
        default="",
        description="核心监管要求（合规义务）:该外规条款对适用主体设定的具体合规义务,"
                    "编号逐条列出、每项一行(空 = 条款为定义/目的/适用范围/附则性质,"
                    "或无具体义务)",
    )
    retrieved_candidates: list[RetrievedCandidate] = Field(
        default_factory=list,
        description="Top-k internal clauses batch-pre-retrieved for this external "
                    "clause (display-only review evidence, not part of the episode)",
    )


class CompareResult(BaseModel):
    """Full comparison result — many-to-many, keyed by document name lists."""

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
        """覆盖率分母:需要内化 + 待判定(空值保守计入)。"""
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
        """(完全覆盖 + 部分覆盖) / 需内化条款数;分母为 0 时视为 1.0。"""
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
