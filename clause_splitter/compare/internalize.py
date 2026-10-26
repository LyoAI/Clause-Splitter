"""Per-clause internalization judgment — the standalone stage-3 core.

Judged from the clause text alone (no internal-library retrieval). Extracted
from ``aggregate._pre_internalize`` so the service pipeline can run it as its
own stage; ``aggregate`` delegates to :func:`judge_internalization`. Failure is
fail-open: fields stay empty and the clause is conservatively treated as
"needs internalization" downstream.
"""

from __future__ import annotations

import asyncio
from typing import Any

from clause_splitter.models import normalize_internalization_decision
from clause_splitter.prompts import render
from clause_splitter.utils import get_logger, norm_str
from .episode import ExternalClause
from .ground import parse_answer_json

logger = get_logger()


def build_judge_prompt(clause: ExternalClause) -> str:
    """User message judging one external clause (no retrieval)."""
    return (
        "【外部规定条款】\n"
        f"制度名称: {clause.document_name}\n"
        f"条款号: {clause.clause_number or '全文'}\n"
        f"条款原文:\n{clause.full_text}\n\n"
        "请按内化判定框架与输出契约,判定该条款是否需要内化。"
    )


async def judge_internalization(llm: Any, clause: ExternalClause, timeout_s: float) -> None:
    """Judge one clause in place (clause_type / decision / reason written back)."""
    try:
        resp = await asyncio.wait_for(
            llm.ainvoke([
                {"role": "system", "content": render("internalization")},
                {"role": "user", "content": build_judge_prompt(clause)},
            ]),
            timeout=timeout_s,
        )
        content = resp.content if hasattr(resp, "content") else str(resp)
        parsed = parse_answer_json(content)
        clause.clause_type = norm_str(parsed.get("clause_type") if parsed else "")
        # 预防性内化 → 需要内化(写入点归一,下游一致)。
        clause.internalization_decision = normalize_internalization_decision(
            norm_str(parsed.get("internalization_decision") if parsed else "")
        )
        clause.internalization_reason = norm_str(parsed.get("internalization_reason") if parsed else "")
    except Exception as e:  # noqa: BLE001 — 判定失败不拖垮整批
        logger.warning("internalization judgment failed (%s): %s", clause.clause_id, e)
