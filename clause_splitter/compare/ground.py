"""Grounding — turn the agent's verdict into a verifiable MatchResult.

Hard-learned lesson: the "batch 0% coverage" bug — the agent claimed coverage
but cited internal-clause IDs that never appeared in retrieval. Three lines of
defense:
1. The answer contract only allows chunk_ids seen in the trajectory
   (prompts/analysis_agent.md iron rule).
2. ``ground_episode`` hard-verifies: every claimed id must be ∈ seen set AND
   ∈ store; unverifiable claims are dropped.
3. A coverage verdict (full/partial) with no grounded claim degrades to
   缺失, with the analysis wholesale replaced by the downgrade text so no
   invented citation can leak into the gap report.
"""

from __future__ import annotations

import json
import re
from typing import Any

from chillrag.tools.searcher.store import DocumentStore

from clause_splitter.models import CoverageStatus, normalize_internalization_decision
from clause_splitter.utils import get_logger
from .episode import EpisodeResult, ExternalClause

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
    """Extract the JSON from the final message's ``<answer>…</answer>`` block.

    Returns None on failure (caller decides how to degrade).
    """
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
    """Union the chunk_ids across all ``*_search`` / ``get_context`` tool results.

    This is grounding's candidate universe — any chunk_id never returned by a
    tool is treated as a hallucination.
    """
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
    result: EpisodeResult,
    store: DocumentStore,
    ext: ExternalClause,
) -> dict[str, Any]:
    """Convert one episode into a verifiable MatchResult dict.

    Args:
        result: The finished episode.
        store: Internal clause library (single source of text).
        ext: External clause (fills provenance + source text).

    Returns:
        MatchResult fields; coverage claims that cannot be grounded degrade
        to 缺失.
    """
    parsed = parse_answer_json(result.raw_answer)
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
        # Internalization fields are produced by the pre-retrieval stage into
        # ext — the episode no longer emits them. Missing/non-str → "".
        "clause_type": _norm_str(ext.clause_type),
        "internalization_decision": normalize_internalization_decision(_norm_str(ext.internalization_decision)),
        "internalization_reason": _norm_str(ext.internalization_reason),
        # difference_analysis / core_regulatory_requirements / remedy_suggestion
        # still come from the episode (differences depend on the comparison;
        # core requirements extract obligations; remedies target gaps).
        "difference_analysis": _norm_str(parsed.get("difference_analysis") if parsed else ""),
        "core_regulatory_requirements": _norm_str(parsed.get("core_regulatory_requirements") if parsed else ""),
        "remedy_suggestion": _norm_str(parsed.get("remedy_suggestion") if parsed else ""),
    }

    # 缺失: nothing adopted — return as-is (claimed ids are never grounded).
    if status == CoverageStatus.NOT_COVERED:
        if not base["analysis"].strip():
            base["analysis"] = _NOT_COVERED_ANALYSIS
        if not base["remedy_suggestion"].strip():
            base["remedy_suggestion"] = _NOT_COVERED_REMEDY
        return base

    # A coverage verdict → grounding is mandatory.
    seen = collect_seen_chunk_ids(result.trajectory)
    grounded: list[str] = []
    evidence: dict[str, str] = {}
    for cid in claimed_ids:
        # Double hard check: ∈ seen set AND ∈ store; fail either → drop.
        if cid not in seen:
            logger.warning("claim %r not in retrieval trajectory, dropped", cid)
            continue
        chunk = store.get_chunk(cid)
        if chunk is None:
            logger.warning("claim %r not in store, dropped", cid)
            continue
        grounded.append(cid)
        # Evidence quotes the store verbatim — never the agent's paraphrase.
        ev = None
        if parsed:
            ev = parsed.get("evidence", {}).get(cid)
        evidence[cid] = str(ev or chunk.text[:200])

    if not grounded:
        logger.warning(
            "ext=%s status=%s with no groundable claim, degrading to missing",
            ext.clause_id, status,
        )
        return {
            **base,
            "status": CoverageStatus.NOT_COVERED,
            "analysis": _DOWNGRADE_ANALYSIS,
        }

    # The first adopted clause fills the internal-side fields (report display);
    # the full list goes to matched_internal_clause_ids (review page marks all
    # of them "✓ adopted"). Chunk layout is hierarchical: doc_id = 制度名,
    # clause label = last section-path leaf (e.g. "第二十条"), with order
    # fallback for untitled docs.
    head = store.get_chunk(grounded[0])
    return {
        **base,
        "status": status,
        "matched_internal_id": grounded[0],
        "matched_internal_document_name": head.doc_id if head else None,
        "matched_internal_clause_number": _segment_label(head) if head else "",
        "matched_internal_source_text": head.text if head else "",
        "matched_internal_clause_ids": grounded,
        "analysis": _merge_analysis(analysis, grounded, store),
    }


# ── normalization helpers ─────────────────────────────────────────────


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


def _norm_str(value: Any) -> str:
    """Lenient str getter: missing / non-str → \"\" so new fields never break a run."""
    if isinstance(value, str):
        return value.strip()
    return ""


def _segment_label(chunk) -> str:
    """The internal chunk's clause/paragraph label.

    Prefers the last leaf of ``meta.section`` ("第三章 数据安全 > 第二十条" →
    "第二十条"; Excel libraries store "第一条" directly — returned as-is).
    Without a section, extract a leading "第X条" from the chunk text; last
    resort is a 1-based paragraph number (``第N段``) aligned with real numbering.
    """
    section = str(chunk.meta.get("section", "") or "").strip() if chunk.meta else ""
    if section:
        leaf = section.split(" > ")[-1].strip()
        if leaf:
            return leaf
    head = re.match(r"^\s*(第[一二三四五六七八九十百零\d]+条)", chunk.text or "")
    if head:
        return head.group(1)
    if "#" in chunk.chunk_id:
        order = chunk.chunk_id.rsplit("#", 1)[-1]
        if order.isdigit():
            return f"第{int(order) + 1}段"
    return ""


def _merge_analysis(analysis: str, grounded: list[str], store: DocumentStore) -> str:
    """Fall back to the adopted-clause list when the analysis is empty.

    A non-empty analysis is never appended to — adoption is already shown by
    the review page's "✓ 采用内规" and the report's 承接内规 column. The
    fallback renders each grounded chunk_id as ``制度名 第X条`` (same
    _segment_label numbering as the review page) so 0-based chunk ids
    ("…#0") never leak into user-facing text.
    """
    if analysis:
        return analysis
    refs = []
    for cid in grounded:
        chunk = store.get_chunk(cid)
        doc = chunk.doc_id if chunk else ""
        label = _segment_label(chunk) if chunk else ""
        refs.append(f"{doc} {label}".strip() or cid)
    return "承接内规条款: " + ", ".join(refs)
