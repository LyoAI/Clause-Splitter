"""Turn the episode's ``<answer>`` JSON into a verdict.

The verdict is taken at face value: claimed internal chunk ids are never used to
change the status. Zeus ids are long opaque strings the model rarely copies
exactly, so ids only fill display fields (doc name, clause name, source text) and
an unresolvable id leaves those fields empty.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from rag import ChunkLookup
from utils import get_logger, norm_str

from .types import CoverageStatus

logger = get_logger()

_NOT_COVERED_ANALYSIS = "内规库中未检索到承接该外规义务的条款，判定为缺失。"
_NOT_COVERED_REMEDY = "建议新增或修订内规条款，承接该外规义务。"

_ANSWER_RE = re.compile(r"<answer>(.*?)</answer>", re.DOTALL)
_JSON_DECODER = json.JSONDecoder()


@dataclass
class Verdict:
    status: CoverageStatus
    claimed_ids: list[str] = field(default_factory=list)
    analysis: str = ""
    difference_analysis: str = ""
    core_requirements: str = ""
    remedy_suggestion: str = ""


def parse_answer_json(raw: str) -> dict[str, Any] | None:
    """First JSON object inside ``<answer>…</answer>`` (or in the raw text).

    Tolerates a missing closing tag, prose around the block and trailing text.
    Returns ``None`` when nothing parses; the caller degrades to defaults.
    """
    m = _ANSWER_RE.search(raw or "")
    body = m.group(1).strip() if m else (raw or "").strip()
    if not body:
        return None
    start = body.find("{")
    if start == -1:
        return _unparseable(body)
    try:
        obj, _ = _JSON_DECODER.raw_decode(body, start)
    except json.JSONDecodeError:
        return _unparseable(body)
    return obj if isinstance(obj, dict) else _unparseable(body)


def _unparseable(body: str) -> None:
    logger.warning("answer JSON unparseable (%d chars, head=%r)", len(body), body[:60])
    return None


def ground(raw_answer: str) -> Verdict:
    """Build a verdict from the episode answer. Never raises on bad content."""
    parsed = parse_answer_json(raw_answer) or {}
    status = _norm_status(parsed.get("status"))
    verdict = Verdict(
        status=status,
        claimed_ids=_norm_ids(parsed.get("matched_internal_clause_ids")),
        analysis=norm_str(parsed.get("analysis")),
        difference_analysis=norm_str(parsed.get("difference_analysis")),
        core_requirements=norm_str(parsed.get("core_regulatory_requirements")),
        remedy_suggestion=norm_str(parsed.get("remedy_suggestion")),
    )
    if status == CoverageStatus.NOT_COVERED:
        verdict.analysis = verdict.analysis or _NOT_COVERED_ANALYSIS
        verdict.remedy_suggestion = verdict.remedy_suggestion or _NOT_COVERED_REMEDY
    return verdict


def resolve_ids(ids: list[str], lookup: ChunkLookup) -> list[tuple[str, Any]]:
    """Pair each claimed id with its cached chunk (``None`` when unresolvable)."""
    return [(cid, lookup.get_chunk(cid)) for cid in ids]


def _norm_status(value: Any) -> CoverageStatus:
    text = str(value or "").strip()
    for status in CoverageStatus:
        if text in (status.value, status.name):
            return status
    logger.warning("unknown coverage status %r, treating as missing", value)
    return CoverageStatus.NOT_COVERED


def _norm_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return []
