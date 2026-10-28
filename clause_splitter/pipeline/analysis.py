"""Stage 4 — per-article analysis: ReAct compare + review page.

Per 需要内化 clause: ``run_compare`` episode with the stage-3 decision
prefilled (no re-billing) → ``compare_result.json`` → review HTML →
MatchResults persisted back into clause rows (all fields the review page shows).
"""

from __future__ import annotations

import asyncio
from typing import Any

from clause_splitter.compare.aggregate import run_compare
from clause_splitter.compare.episode import ExternalClause
from clause_splitter.compare.review import render_review_html
from clause_splitter.models import CompareResult
from clause_splitter.rag import RagError, RagService, get_rag_service
from clause_splitter.utils import get_logger
from .contexts import StageContext, StageError

logger = get_logger()

_NO_NEED = "无需内化"


def _index_signature(service: RagService) -> str:
    """Corpus version reported by the RAG service (empty when it can't tell)."""
    try:
        return service.stats().version or ""
    except RagError as exc:
        logger.warning("rag stats failed while stamping the analysis: %s", exc)
        return ""


def _row_updates(match: Any) -> dict[str, Any]:
    """MatchResult → clause-row fields (matched text snapshotted for stability)."""
    return {
        "status": match.status.value,
        "analysis": match.analysis,
        "difference_analysis": match.difference_analysis,
        "core_regulatory_requirements": match.core_regulatory_requirements,
        "remedy_suggestion": match.remedy_suggestion,
        "matched_internal_id": match.matched_internal_id or "",
        "matched_internal_document_name": match.matched_internal_document_name or "",
        "matched_internal_clause_number": match.matched_internal_clause_number,
        "matched_internal_source_text": match.matched_internal_source_text,
        "matched_internal_clause_ids": match.matched_internal_clause_ids,
        "candidates": [c.model_dump(mode="json") for c in match.retrieved_candidates],
    }


async def run(ctx: StageContext) -> dict[str, Any]:
    rows, _total = await ctx.repos.clauses.list_for_regulation(ctx.reg_id)
    if not rows:
        raise StageError("没有可分析的条款 —— 请先执行拆条与内化判定。")

    service = ctx.rag or get_rag_service()
    if not service.available:
        raise StageError("RAG 服务未配置 —— 请检查 RAG_PROVIDER 与连接配置（RAG_API_BASE / ZEUS_*）后重启服务。")

    title = ctx.regulation.get("title") or ""
    target = [r for r in rows if (r.get("internalization_decision") or "").strip() != _NO_NEED]
    logger.info(
        "analysis stage: %d/%d clauses enter episodes for %s", len(target), len(rows), ctx.reg_id
    )

    clauses = [
        ExternalClause(
            document_name=title,
            clause_number=r["clause_number"],
            section=r.get("section") or "",
            full_text=r["clause_text"],
            clause_type=r.get("clause_type") or "",
            internalization_decision=r.get("internalization_decision") or "",
            internalization_reason=r.get("internalization_reason") or "",
        )
        for r in rows
    ]

    # episodes — one per clause; 无需内化 rows skip inside run_compare.
    tick = max(len(rows) // 10, 1)

    def hook(clauses_done: int, total: int) -> None:
        ctx.progress(0.9 * clauses_done / max(total, 1), f"条款比对 {clauses_done}/{total}")
        if clauses_done == total or clauses_done % tick == 0:
            logger.info(
                "analysis progress: %d/%d 条款 (%.0f%%)",
                clauses_done, total, 100.0 * clauses_done / max(total, 1),
            )

    result: CompareResult = await run_compare(
        clauses, service, ctx.llm,
        persist_dir=str(ctx.reg_dir / "episodes"),
        progress_hook=hook,
    )

    reg_dir = ctx.reg_dir
    reg_dir.mkdir(parents=True, exist_ok=True)

    # 1) raw result + review page.
    (reg_dir / "compare_result.json").write_text(
        result.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
    )
    render_review_html(result, reg_dir / "内规检索复核.html", store_name=service.name)

    # 2) matches ↔ rows are order-aligned by construction (one episode per row).
    for row, match in zip(rows, result.matches):
        await ctx.repos.clauses.update(row["_id"], _row_updates(match))

    summary = {
        "fully_covered_count": result.fully_covered_count,
        "partially_covered_count": result.partially_covered_count,
        "missing_count": result.internalizable_missing_count,
        "internalizable_count": result.internalizable_count,
        "no_internalization_count": result.no_internalization_count,
    }
    logger.info(
        "analysis stage done for %s: coverage=%.1f%%",
        ctx.reg_id, result.coverage_rate * 100,
    )
    signature = await asyncio.to_thread(_index_signature, service)
    return {"analysis_summary": summary, "analysis_index_signature": signature}
