"""Grounding — turn the agent's verdict into a verifiable MatchResult.

Every claimed internal chunk id must appear in the retrieval trajectory AND
resolve from this run's chunk cache. A coverage verdict without a groundable
claim degrades to 缺失 with the analysis replaced, so hallucinated citations
never reach the gap report.
"""

from __future__ import annotations

import json
import re
from typing import Any

from clause_splitter.agent import AgentResult
from clause_splitter.models import CoverageStatus, MatchResult, normalize_internalization_decision
from clause_splitter.rag import ChunkLookup
from clause_splitter.utils import get_logger, norm_str, segment_label

from .episode import ExternalClause

logger = get_logger()

# Surfaces in the review page / report — keep user-facing wording.
_DOWNGRADE_ANALYSIS = (
    "内规库中未检索到可承接的候选条款。判定声称已覆盖，但引用的内规条款 ID "
    "无法对应到任何检索结果（疑似幻觉），已降级为缺失。请人工复核该外规义务是否确有内规承接。"
)
#: Default analysis when the agent gave none (MatchResult.analysis must be non-empty).
_NOT_COVERED_ANALYSIS = "内规库中未检索到承接该外规义务的条款，判定为缺失。"
#: Default remedy for 缺失 when the agent gave none.
_NOT_COVERED_REMEDY = "建议新增或修订内规条款，承接该外规义务。"

_ANSWER_RE = re.compile(r"<answer>(.*?)</answer>", re.DOTALL)


def parse_answer_json(raw_answer: str) -> dict[str, Any] | None:
    """Extract the JSON from the final message's ``<answer>…</answer>`` block."""
    m = _ANSWER_RE.search(raw_answer)
    body = m.group(1).strip() if m else raw_answer.strip()
    if not body:
        return None
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def collect_seen_chunk_ids(trajectory: list) -> set[str]:
    """Union the chunk_ids across all ``*_search`` / ``get_context`` tool results."""
    seen: set[str] = set()
    for turn in trajectory:
        if not turn.tool_name.endswith("_search") and turn.tool_name != "get_context":
            continue
        try:
            rows = json.loads(turn.tool_result)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("chunk_id"):
                seen.add(str(row["chunk_id"]))
    return seen


def ground_episode(
    result: AgentResult,
    lookup: ChunkLookup,
    ext: ExternalClause,
) -> MatchResult:
    """Convert one episode into a MatchResult, degrading ungrounded claims."""
    parsed = parse_answer_json(result.answer)
    status = _norm_status(parsed.get("status") if parsed else None)
    claimed_ids = _norm_claimed_ids(parsed.get("matched_internal_clause_ids") if parsed else None)
    analysis = str(parsed.get("analysis") or "") if parsed else ""

    base = {
        "external_clause_id": ext.clause_id,
        "external_document_name": ext.document_name,
        "external_article_id": ext.clause_number,
        "external_source_text": ext.full_text,
        "status": status,
        "matched_internal_id": None,
        "matched_internal_document_name": None,
        "matched_internal_clause_number": "",
        "matched_internal_source_text": "",
        "analysis": analysis,
        "clause_type": norm_str(ext.clause_type),
        "internalization_decision": normalize_internalization_decision(
            norm_str(ext.internalization_decision)
        ),
        "internalization_reason": norm_str(ext.internalization_reason),
        "difference_analysis": norm_str(parsed.get("difference_analysis") if parsed else ""),
        "core_regulatory_requirements": norm_str(
            parsed.get("core_regulatory_requirements") if parsed else ""
        ),
        "remedy_suggestion": norm_str(parsed.get("remedy_suggestion") if parsed else ""),
    }

    if status == CoverageStatus.NOT_COVERED:
        if not base["analysis"].strip():
            base["analysis"] = _NOT_COVERED_ANALYSIS
        if not base["remedy_suggestion"].strip():
            base["remedy_suggestion"] = _NOT_COVERED_REMEDY
        return MatchResult(**base)

    # A coverage verdict → grounding is mandatory.
    seen = collect_seen_chunk_ids(result.trajectory)
    grounded: list[str] = []
    for cid in claimed_ids:
        if cid not in seen:
            logger.warning("claim %r not in retrieval trajectory, dropped", cid)
            continue
        chunk = lookup.get_chunk(cid)
        if chunk is None:
            logger.warning("claim %r not in store, dropped", cid)
            continue
        grounded.append(cid)

    if not grounded:
        logger.warning(
            "ext=%s status=%s with no groundable claim, degrading to missing",
            ext.clause_id, status,
        )
        return MatchResult(
            **{**base, "status": CoverageStatus.NOT_COVERED, "analysis": _DOWNGRADE_ANALYSIS}
        )

    head = lookup.get_chunk(grounded[0])
    return MatchResult(**{
        **base,
        "matched_internal_id": grounded[0],
        "matched_internal_document_name": head.doc_id if head else None,
        "matched_internal_clause_number": segment_label(head) if head else "",
        "matched_internal_source_text": head.text if head else "",
        "matched_internal_clause_ids": grounded,
        "analysis": _merge_analysis(analysis, grounded, lookup),
    })


def _norm_status(value: Any) -> CoverageStatus:
    """Normalize the agent's status string to a CoverageStatus."""
    if value is None:
        return CoverageStatus.NOT_COVERED
    s = str(value).strip()
    for status in CoverageStatus:
        if s in (status.value, status.name):
            return status
    logger.warning("unknown coverage status %r, treating as missing", value)
    return CoverageStatus.NOT_COVERED


def _norm_claimed_ids(value: Any) -> list[str]:
    """Normalize claimed ids (comma-joined string or list)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def _merge_analysis(analysis: str, grounded: list[str], lookup: ChunkLookup) -> str:
    """Fall back to the adopted-clause list when the analysis is empty."""
    if analysis:
        return analysis
    refs = []
    for cid in grounded:
        chunk = lookup.get_chunk(cid)
        doc = chunk.doc_id if chunk else ""
        label = segment_label(chunk) if chunk else ""
        refs.append(f"{doc} {label}".strip() or cid)
    return "承接内规条款: " + ", ".join(refs)
