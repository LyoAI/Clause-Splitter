"""Grounding — turn the agent's verdict into a MatchResult.

The episode's coverage verdict is taken at face value: claimed internal chunk
ids are **not** checked against the retrieval trajectory. Zeus chunk ids are
long opaque strings that the model rarely reproduces exactly, so a strict check
turned real coverage into 缺失. Ids are resolved against this run's chunk cache
for display only (doc name / clause number / source text); an unresolvable id
leaves those fields empty but never changes the status.
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

#: Default analysis when the agent gave none (MatchResult.analysis must be non-empty).
_NOT_COVERED_ANALYSIS = "内规库中未检索到承接该外规义务的条款，判定为缺失。"
#: Default remedy for 缺失 when the agent gave none.
_NOT_COVERED_REMEDY = "建议新增或修订内规条款，承接该外规义务。"

_ANSWER_RE = re.compile(r"<answer>(.*?)</answer>", re.DOTALL)
#: One-value decoder — ``raw_decode`` ignores content after the object, so
#: trailing prose never costs the verdict.
_JSON_DECODER = json.JSONDecoder()


def parse_answer_json(raw_answer: str) -> dict[str, Any] | None:
    """Extract the JSON object from the final answer's ``<answer>…</answer>``.

    Contract deviations are tolerated before giving up: a missing closing
    tag (some models stop right after the JSON — the verdict would otherwise
    silently degrade to 缺失), prose around the block, trailing text. Only a
    genuinely unparseable answer returns None, and that is logged: every
    caller degrades None the same quiet way, so this is the one visible seam.
    """
    m = _ANSWER_RE.search(raw_answer)
    body = m.group(1).strip() if m else raw_answer.strip()
    if not body:
        return None
    parsed = _first_json_object(body)
    if parsed is not None:
        return parsed
    logger.warning(
        "answer JSON unparseable (%d chars, head=%r) — fields degrade to defaults",
        len(body), body[:60],
    )
    return None


def _first_json_object(text: str) -> dict[str, Any] | None:
    """Parse ``text`` as one JSON object, else salvage the first object in it.

    Scanning from the first ``{`` skips an unclosed ``<answer>`` prefix and
    any prose; ``raw_decode`` then stops at the object's end.
    """
    start = text.find("{")
    if start == -1:
        return None
    try:
        obj, _end = _JSON_DECODER.raw_decode(text, start)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


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

    # A coverage verdict passes through; ids only feed the display fields.
    claimed = claimed_ids
    resolved = [cid for cid in claimed if lookup.get_chunk(cid)]
    head_id = resolved[0] if resolved else (claimed[0] if claimed else None)
    head = lookup.get_chunk(head_id) if head_id else None
    if claimed and not resolved:
        logger.warning(
            "ext=%s claims %s, none resolvable from this run's cache — "
            "display fields left empty, verdict kept",
            ext.clause_id, claimed,
        )
    return MatchResult(**{
        **base,
        "matched_internal_id": head_id,
        "matched_internal_document_name": head.doc_id if head else None,
        "matched_internal_clause_number": segment_label(head) if head else "",
        "matched_internal_source_text": head.text if head else "",
        "matched_internal_clause_ids": claimed,
        "analysis": _merge_analysis(analysis, claimed, lookup),
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
