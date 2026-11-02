"""Input and output models of the clause-analysis module.

``ClauseResult.to_esg()`` emits the algorithm-owned fields of one ``data[]``
element of the backend payload (see ``assets/callback-contract.md``). Document
and orchestration fields (chapter, orderNum, termId, termContent, ...) belong to
the caller.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

#: Internalization decision — the only two values the contract allows.
NEEDS_INTERNALIZATION = "需要内化"
NO_INTERNALIZATION = "无需内化"


class CoverageStatus(str, Enum):
    FULLY_COVERED = "完全覆盖"
    PARTIALLY_COVERED = "部分覆盖"
    NOT_COVERED = "缺失"


class ClauseInput(BaseModel):
    """One already-split external clause."""

    document_name: str = Field(default="", description="外规名称(后端 docId 对应的法规名)")
    clause_number: str = Field(default="", description="条款号,如 第一条 / Article 1")
    section: str = Field(default="", description="所属章节路径,无则空串")
    text: str = Field(..., min_length=1, description="条款原文")


class RelatedTerm(BaseModel):
    """An internal clause the episode adopted as evidence."""

    internal_clause_id: str
    doc_name: str = ""
    clause_name: str = ""
    clause_content: str = ""


class ClauseResult(BaseModel):
    """Outcome for one clause.

    ``coverage_status`` is ``None`` when no analysis ran (the clause does not
    need internalization); the ESG payload then carries an empty string.
    """

    clause_number: str = ""
    internalize: str = Field(..., description="需要内化 / 无需内化")
    internalize_reason: str = ""
    coverage_status: CoverageStatus | None = None
    analysis: str = ""
    difference_analysis: str = ""
    core_requirements: str = ""
    remedy_suggestion: str = ""
    related_terms: list[RelatedTerm] = Field(default_factory=list)

    def to_esg(self) -> dict[str, Any]:
        """Algorithm-owned fields of one ``data[]`` element."""
        return {
            "termName": self.clause_number,
            "internalize": self.internalize,
            "internalizeReason": self.internalize_reason,
            "coverageStatus": self.coverage_status.value if self.coverage_status else "",
            "analysis": self.analysis,
            "differenceAnalysis": self.difference_analysis,
            "coreRequirements": self.core_requirements,
            "remedySuggestion": self.remedy_suggestion,
            "relationTerms": [
                {
                    "internalClauseId": t.internal_clause_id,
                    "docName": t.doc_name,
                    "clauseName": t.clause_name,
                    "clauseContent": t.clause_content,
                }
                for t in self.related_terms
            ],
        }
