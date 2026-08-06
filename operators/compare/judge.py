"""Per-obligation LLM judge for the compare pipeline.

Replaces the ReAct batch agent. Each external obligation gets ONE focused LLM
call carrying exactly the candidates BM25 retrieved. The judge outputs a small
JSON with a candidate ROW NUMBER (not an obligation id) — the pipeline grounds
it in :mod:`operators.compare.validation`. Because the model can only ever
reference a retrieved candidate, id fabrication is structurally impossible.
"""

from __future__ import annotations

import json
import re
from typing import Any

from prompts.compare.judge import JUDGE_SYSTEM_PROMPT, build_judge_user_prompt
from utils import get_logger

logger = get_logger()

_VALID_STATUSES = {"完全覆盖", "部分覆盖", "缺失"}


def build_judge_messages(
    external_obl: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Assemble the system + user messages for one judgment call."""
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_judge_user_prompt(external_obl, candidates),
        },
    ]


def resolve_candidate_index(
    value: Any,
    candidates: list[dict[str, Any]],
) -> int | None:
    """Map the model's answer to a 1-based candidate row, or None.

    Accepts:
      - an int row number in [1, len(candidates)]
      - a numeric string row number
      - a full composite id matching a candidate (``制度名::序号``) — dual
        acceptance so a model that ignored the schema can still be salvaged
    Anything else (a fabricated id, an out-of-range number) → None.
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if 1 <= value <= len(candidates) else None

    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            n = int(s)
            return n if 1 <= n <= len(candidates) else None
        for i, candidate in enumerate(candidates, start=1):
            obl = candidate.get("obligation", {})
            doc = str(obl.get("document_name", ""))
            idx = str(obl.get("index", ""))
            if s in {f"{doc}::{idx}", f"{doc}::第{idx}条"}:
                return i
        return None

    return None


def parse_judge_response(
    content: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Parse the judge's JSON output into a normalized dict.

    Returns:
        ``{external_clause_id, status, matched_candidate_index, analysis,
        remedy_suggestion}``, or None when the content cannot be parsed.
        ``matched_candidate_index`` is a validated 1-based row or None.
    """
    if not content:
        return None

    parsed = _extract_json_object(content)
    if parsed is None:
        return None

    status_raw = str(parsed.get("status", "")).strip()
    status = status_raw if status_raw in _VALID_STATUSES else "缺失"
    cand_index = resolve_candidate_index(
        parsed.get("matched_candidate_index"), candidates,
    )

    return {
        "external_clause_id": str(parsed.get("external_clause_id", "") or ""),
        "status": status,
        "matched_candidate_index": cand_index,
        "analysis": str(parsed.get("analysis", "") or ""),
        "remedy_suggestion": str(parsed.get("remedy_suggestion", "") or ""),
    }


def _extract_json_object(content: str) -> dict | None:
    """Extract a JSON object from LLM output.

    Handles fenced code blocks, a bare object, or the first ``{...}`` substring.
    """
    # Fenced code block (```json ... ```)
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", content)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Raw object parse of the whole content
    try:
        obj = json.loads(content.strip())
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # First {...} block
    m = re.search(r"\{[\s\S]+\}", content)
    if m:
        try:
            obj = json.loads(m.group())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    return None


async def judge_obligation(
    llm: Any,
    external_obl: dict[str, Any],
    candidates: list[dict[str, Any]],
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Judge ONE external obligation against its retrieved candidates.

    Args:
        llm: LLM instance supporting ``ainvoke(messages)`` (or ``invoke``).
        external_obl: One external ClauseObligation dict.
        candidates: Retrieved candidates (``{docid, score, obligation}``).
        max_attempts: Number of LLM attempts before falling back to 缺失.

    Returns:
        A normalized judge dict (see :func:`parse_judge_response`). Never
        raises — transient LLM failures degrade to a labelled 缺失 result.
    """
    messages = build_judge_messages(external_obl, candidates)
    last_error = ""

    for attempt in range(max_attempts):
        try:
            if hasattr(llm, "ainvoke"):
                response = await llm.ainvoke(messages)
            else:
                response = llm.invoke(messages)
            content = (
                response.content
                if hasattr(response, "content")
                else str(response)
            )
        except Exception as e:  # noqa: BLE001 — a bad call must not kill the batch
            last_error = str(e)
            logger.warning(
                "Judge attempt %d/%d failed: %s", attempt + 1, max_attempts, e,
            )
            continue

        result = parse_judge_response(content, candidates)
        if result is not None:
            return result

    logger.error(
        "Judge failed after %d attempts (last error: %s)",
        max_attempts, last_error,
    )
    return {
        "external_clause_id": str(external_obl.get("index", "")),
        "status": "缺失",
        "matched_candidate_index": None,
        "analysis": "LLM 未能完成判定（多次尝试失败）",
        "remedy_suggestion": "请人工审核此项",
    }
