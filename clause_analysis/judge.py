"""Internalization judgment for one clause — text only, no retrieval.

Fail-open: any LLM or parsing failure yields "需要内化" with the reason recorded,
so an unjudged clause is never silently dropped from the analysis.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from utils import get_logger, norm_str

from .grounding import parse_answer_json
from .prompts import render
from .types import NEEDS_INTERNALIZATION, NO_INTERNALIZATION, ClauseInput

logger = get_logger()

_REQUIRE_TYPES = ("义务性要求", "禁止性要求", "鼓励性要求")


@dataclass
class Judgment:
    decision: str
    reason: str = ""
    clause_type: str = ""
    require_type: str = ""


def _fallback(reason: str) -> Judgment:
    return Judgment(decision=NEEDS_INTERNALIZATION, reason=reason)


async def judge(llm: Any, clause: ClauseInput, timeout_s: float) -> Judgment:
    messages = [
        {"role": "system", "content": render("internalization")},
        {"role": "user", "content": _judge_prompt(clause)},
    ]
    try:
        resp = await asyncio.wait_for(llm.ainvoke(messages), timeout=timeout_s)
    except Exception as e:  # noqa: BLE001 — fail-open, one clause must not stop the batch
        logger.warning("internalization judgment failed (%s): %s", clause.term_name, e)
        return _fallback("内化判定失败,按需要内化处理。")

    parsed = parse_answer_json(resp.content or "")
    if parsed is None:
        return _fallback("内化判定输出无法解析,按需要内化处理。")
    # Anything but the explicit 无需内化 is treated as needing internalization.
    decision = norm_str(parsed.get("internalization_decision"))
    require_type = norm_str(parsed.get("requireType"))
    return Judgment(
        decision=NO_INTERNALIZATION if decision == NO_INTERNALIZATION else NEEDS_INTERNALIZATION,
        reason=norm_str(parsed.get("internalization_reason")),
        clause_type=norm_str(parsed.get("clause_type")),
        require_type=require_type if require_type in _REQUIRE_TYPES else "",
    )


def _judge_prompt(clause: ClauseInput) -> str:
    return (
        "【外部规定条款】\n"
        f"制度名称: {clause.document_name}\n"
        f"条款名称: {clause.term_name or '全文'}\n"
        f"条款原文:\n{clause.term_content}\n\n"
        "请按内化判定框架与输出契约,判定该条款是否需要内化。"
    )
