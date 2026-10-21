"""Stage 3 — per-article internalization judgment (外规内化判定).

Judges every clause from the split stage via
``compare.internalize.judge_internalization`` (fail-open: a failed judgment
leaves the decision empty ⇒ conservatively treated as 需要内化 downstream).
"""

from __future__ import annotations

import asyncio
from typing import Any

from clause_splitter.compare.episode import ExternalClause
from clause_splitter.compare.internalize import judge_internalization
from clause_splitter.config import COMPARE_CONCURRENCY, EPISODE_TIMEOUT_S
from clause_splitter.utils import get_logger
from .contexts import StageContext, StageError

logger = get_logger()


async def run(ctx: StageContext) -> dict[str, Any]:
    rows, _total = await ctx.repos.clauses.list_for_regulation(ctx.reg_id)
    if not rows:
        raise StageError("没有可判定的条款 —— 请先执行拆条。")

    total = len(rows)
    sem = asyncio.Semaphore(COMPARE_CONCURRENCY)
    counts = {"need": 0, "no_need": 0}

    async def judge(row: dict[str, Any]) -> None:
        clause = ExternalClause(
            document_name=ctx.regulation.get("title") or "",
            clause_number=row["clause_number"],
            section=row.get("section") or "",
            full_text=row["clause_text"],
        )
        async with sem:
            await judge_internalization(ctx.llm, clause, EPISODE_TIMEOUT_S)
        decision = (clause.internalization_decision or "").strip()
        if decision == "无需内化":
            counts["no_need"] += 1
        else:
            counts["need"] += 1
        await ctx.repos.clauses.update(row["_id"], {
            "clause_type": clause.clause_type,
            "internalization_decision": decision,
            "internalization_reason": clause.internalization_reason,
        })
        ctx.progress(
            (counts["need"] + counts["no_need"]) / total,
            f"内化判定 {counts['need'] + counts['no_need']}/{total}",
        )

    await asyncio.gather(*(judge(r) for r in rows))
    logger.info(
        "internalization stage: %d clauses for %s (need=%d no_need=%d)",
        total, ctx.reg_id, counts["need"], counts["no_need"],
    )
    return {
        "clause_count": total,
        "needs_internalization_count": counts["need"],
        "no_internalization_count": counts["no_need"],
    }
