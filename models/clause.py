"""Core Pydantic models for clause obligation extraction."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Enums ─────────────────────────────────────────────────────────────────

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


# ── Core obligation record ────────────────────────────────────────────────

class ClauseObligation(BaseModel):
    """Single atomic obligation — one verb = one record, no merging.

    Field names are in English. Chinese Excel headers are mapped via
    serialization_alias for export. Use `by_alias=True` when serializing
    to match the Excel template column names.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

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
        ..., min_length=1,
        serialization_alias="义务主体",
        description="D — entity or role that performs the action (most likely one)",
    )
    object: str = Field(
        default="/",
        serialization_alias="义务客体",
        description="E — target of the action (entity/person/matter)",
    )
    obligation_rule: ObligationRule = Field(
        ...,
        serialization_alias="义务规则",
        description="F — '应当' or '禁止'",
    )
    action_verb: str = Field(
        ..., min_length=1,
        serialization_alias="义务动作",
        description="G — single predicate verb, e.g. '负责' '建立' '承担'",
    )
    action_target: str = Field(
        ..., min_length=1,
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
        ..., min_length=1,
        serialization_alias="条款编号",
        description="K — clause reference, e.g. '第十四条'",
    )
    source_text: str = Field(
        ..., min_length=1,
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

    def to_excel_row(self) -> list[Any]:
        """Return 13-column list (A-M) for Excel export."""
        return [
            self.index,
            self.trigger_condition,
            self.trigger_sub_condition,
            self.subject,
            self.object,
            self.obligation_rule.value,
            self.action_verb,
            self.action_target,
            self.method,
            self.document_name,
            self.clause_number,
            self.source_text,
            self.ambiguity_note,
        ]


# ── Split result ──────────────────────────────────────────────────────────

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


# ── Document structure (agent intermediate state) ─────────────────────────

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
