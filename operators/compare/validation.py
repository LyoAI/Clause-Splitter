"""Grounding + validation of judge results into MatchResult dicts.

The judge emits a candidate ROW number, never an obligation id. This module
maps that row back to the retrieved candidate, backfills external provenance
and internal evidence verbatim from the store, and downgrades any "covered"
claim that cannot be grounded to a real candidate to 缺失 (hallucination
guard). Analysis text of a downgraded claim is replaced wholesale so a
fabricated quote never leaks into the gap report.
"""

from __future__ import annotations

from typing import Any

from utils import get_logger

logger = get_logger()

_DOWNGRADE_ANALYSIS = (
    "内规库中未检索到可承接的候选条款。判定声称已覆盖，但引用的候选行号"
    "无法对应到任何检索结果（疑似幻觉），已降级为缺失。请人工复核该外规义务是否确有内规承接。"
)
_DOWNGRADE_REMEDY = "待人工复核：确认是否需要新增或修订内规条款以承接该外部义务。"


def ground_match(
    external_obl: dict[str, Any],
    judge_result: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Turn a judge result into a grounded MatchResult dict.

    Args:
        external_obl: The external obligation that was judged.
        judge_result: Normalized dict from ``judge.parse_judge_response``:
            ``{external_clause_id, status, matched_candidate_index, analysis,
            remedy_suggestion}``.
        candidates: The retrieved candidate list handed to the judge
            (``{docid, score, obligation}``).

    Returns:
        A dict matching ``MatchResult``'s fields. A coverage claim whose row
        number does not point at a real candidate is downgraded to 缺失.
    """
    ext_id = str(external_obl.get("index", ""))
    status = judge_result.get("status", "缺失")
    cand_index = judge_result.get("matched_candidate_index")

    base = {
        "external_clause_id": ext_id,
        "external_document_name": external_obl.get("document_name", "") or "",
        "external_article_id": external_obl.get("clause_number", "") or "",
        "external_source_text": external_obl.get("source_text", "") or "",
        "analysis": judge_result.get("analysis", "") or "",
        "remedy_suggestion": judge_result.get("remedy_suggestion", "") or "",
    }

    if status == "缺失":
        return {**base, "status": "缺失", "matched_internal_id": None,
                "matched_internal_document_name": None,
                "matched_internal_clause_number": "",
                "matched_internal_source_text": ""}

    # status is 完全覆盖 / 部分覆盖 → must ground to a real candidate.
    chosen = None
    if isinstance(cand_index, int) and 1 <= cand_index <= len(candidates):
        chosen = candidates[cand_index - 1]

    if chosen is None:
        logger.warning(
            "Coverage claim for ext '%s' has no valid candidate row (%r) — "
            "downgrading to 缺失",
            ext_id, cand_index,
        )
        return {**base, "status": "缺失", "matched_internal_id": None,
                "matched_internal_document_name": None,
                "matched_internal_clause_number": "",
                "matched_internal_source_text": "",
                "analysis": _DOWNGRADE_ANALYSIS,
                "remedy_suggestion": base["remedy_suggestion"]
                or _DOWNGRADE_REMEDY}

    obligation = chosen.get("obligation", {})
    return {**base, "status": status,
            "matched_internal_id": chosen.get("docid", ""),
            "matched_internal_document_name":
                obligation.get("document_name", "") or "",
            "matched_internal_clause_number":
                obligation.get("clause_number", "") or "",
            "matched_internal_source_text":
                obligation.get("source_text", "") or ""}
