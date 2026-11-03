"""Input and output models of the clause-analysis module.

``ClauseResult.to_esg()`` emits the algorithm-owned fields of one ``data[]``
element of the backend payload: the external clause's own identity
(``termId`` / ``termContent`` / ``termKeyword`` / ``termName`` — not to be
confused with the same-named fields inside ``relationTerms``, which describe
internal clauses) plus the analysis results. Enumerations are emitted as
digit strings (see ``assets/clause-analysis-interface.md``). Document-level
and orchestration fields (chapter, orderNum, applicable, ...) belong to the
caller.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

#: Internalization decision — the only two values the judgment can produce.
NEEDS_INTERNALIZATION = "需要内化"
NO_INTERNALIZATION = "无需内化"

#: Wire codes (digit strings). Missing value / not analysed = "".
INTERNALIZE_CODE = {NO_INTERNALIZATION: "0", NEEDS_INTERNALIZATION: "1"}
REQUIRE_TYPE_CODE = {"义务性要求": "1", "禁止性要求": "2", "鼓励性要求": "3"}


class CoverageStatus(str, Enum):
    FULLY_COVERED = "完全覆盖"
    PARTIALLY_COVERED = "部分覆盖"
    NOT_COVERED = "缺失"


#: Wire codes for coverage. Keyed by the enum member, so renaming a label
#: never silently changes a code.
COVERAGE_CODE = {
    CoverageStatus.NOT_COVERED: "0",
    CoverageStatus.PARTIALLY_COVERED: "1",
    CoverageStatus.FULLY_COVERED: "2",
}


class ClauseInput(BaseModel):
    """One already-split external clause."""

    term_content: str = Field(..., min_length=1, description="条款原文")
    term_name: str = Field(..., description="条款名称(条款号),如 第一条")
    term_id: str = Field(default="", description="条款 ID,原样透传到出参 termId;取不到传空串")
    document_name: str = Field(default="", description="外规名称(后端 docId 对应的法规名)")
    section: str = Field(default="", description="所属章节路径,无则空串")


class RelatedTerm(BaseModel):
    """An internal clause the episode adopted as evidence (``relationTerms`` element)."""

    ai_relation_basis: str = Field(default="", description="AI 关联依据:支撑判定的原文关键句")
    relation_doc_id: str = Field(default="", description="关联内规文档 ID")
    term_content: str = Field(default="", description="关联条款原文")
    term_id: str = Field(default="", description="关联条款 ID(检索返回的 chunk_id)")
    term_keyword: str = Field(default="", description="关联条款关键词(rules 正则;llm 模式由比对阶段提取,缺失回退 rules);取不到为空串")
    term_name: str = Field(default="", description="关联条款名称(条款号)")


class ClauseResult(BaseModel):
    """Outcome for one clause.

    ``coverage_status`` is ``None`` when no analysis ran (the clause does not
    need internalization); ``require_type`` is "" when the judgment did not
    give a valid value. The ESG payload carries "" for both in those cases.
    """

    term_name: str = ""
    term_id: str = ""
    term_content: str = ""
    term_keyword: str = ""
    internalize: str = Field(..., description="需要内化 / 无需内化")
    internalize_reason: str = ""
    require_type: str = Field(default="", description="义务性要求 / 禁止性要求 / 鼓励性要求")
    coverage_status: CoverageStatus | None = None
    analysis: str = ""
    difference_analysis: str = ""
    core_requirements: str = ""
    remedy_suggestion: str = ""
    related_terms: list[RelatedTerm] = Field(default_factory=list)

    def to_esg(self) -> dict[str, Any]:
        """Algorithm-owned fields of one ``data[]`` element (digit-string enums)."""
        return {
            "termId": self.term_id,
            "termContent": self.term_content,
            "termKeyword": self.term_keyword,
            "termName": self.term_name,
            "internalize": INTERNALIZE_CODE.get(self.internalize, ""),
            "internalizeReason": self.internalize_reason,
            "requireType": REQUIRE_TYPE_CODE.get(self.require_type, ""),
            "coverageStatus": COVERAGE_CODE[self.coverage_status] if self.coverage_status else "",
            "analysis": self.analysis,
            "differenceAnalysis": self.difference_analysis,
            "coreRequirements": self.core_requirements,
            "remedySuggestion": self.remedy_suggestion,
            "relationTerms": [
                {
                    "aiRelationBasis": t.ai_relation_basis,
                    "relationDocId": t.relation_doc_id,
                    "termContent": t.term_content,
                    "termId": t.term_id,
                    "termKeyword": t.term_keyword,
                    "termName": t.term_name,
                }
                for t in self.related_terms
            ],
        }
