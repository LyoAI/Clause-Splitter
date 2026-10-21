"""Stage 4 — per-article analysis: obligations + ReAct compare + full report.

Per 需要内化 clause: batch obligation decomposition (cheap, no retrieval) →
``run_compare`` episode with the stage-3 decision prefilled (no re-billing) →
``compare_result.json`` written BEFORE the display-only relevance filter →
gap report (sync LLM inside → thread) → review HTML → MatchResults persisted
back into clause rows (all fields the review page shows).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from clause_splitter.compare.aggregate import run_compare
from clause_splitter.compare.episode import ExternalClause
from clause_splitter.compare.report import generate_gap_report
from clause_splitter.compare.review import filter_relevant_candidates, render_review_html
from clause_splitter.config import (
    COMPARE_CONCURRENCY,
    REVIEW_FILTER_RELEVANCE,
)
from clause_splitter.models import CompareResult
from clause_splitter.split.decompose import decompose_batch
from clause_splitter.split.plan import plan_batches
from clause_splitter.split.structure import estimate_complexity
from clause_splitter.utils import get_logger
from config.config_app import CONFIG_APP
from .contexts import StageContext, StageError

logger = get_logger()

_NO_NEED = "无需内化"


async def _decompose_obligations(
    rows: list[dict[str, Any]], title: str, llm: Any, progress
) -> dict[str, list[dict[str, Any]]]:
    """Batch-decompose obligations for the target rows → grouped by clause_number."""
    articles = [
        {
            "article_id": r["clause_number"],
            "full_text": r["clause_text"],
            "complexity_score": estimate_complexity(r["clause_text"])["complexity_score"],
        }
        for r in rows
    ]
    batches = plan_batches(articles)
    grouped: dict[str, list[dict[str, Any]]] = {}
    done = 0
    sem = asyncio.Semaphore(COMPARE_CONCURRENCY)

    async def run_batch(batch: list[dict[str, Any]]) -> None:
        nonlocal done
        async with sem:
            records = await decompose_batch(batch, document_name=title, llm=llm)
        for rec in records:
            grouped.setdefault(str(rec.get("clause_number") or ""), []).append(rec)
        done += 1
        progress(done / max(len(batches), 1), f"义务拆分 {done}/{len(batches)} 批")

    await asyncio.gather(*(run_batch(b) for b in batches))
    return grouped


def _index_signature(store: Any) -> str:
    """Cheap corpus fingerprint: hash over chunk ids (positional ids move on edit)."""
    sha = hashlib.sha256()
    for chunk in store.all_chunks():
        sha.update(chunk.chunk_id.encode("utf-8"))
        sha.update(b"\n")
    return sha.hexdigest()[:16]


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

    store = ctx.store_loader() if ctx.store_loader else None
    if store is None:
        raise StageError("内规索引不可用 —— 请先构建内规库索引。")

    title = ctx.regulation.get("title") or ""
    target = [r for r in rows if (r.get("internalization_decision") or "").strip() != _NO_NEED]
    logger.info(
        "analysis stage: %d/%d clauses enter episodes for %s", len(target), len(rows), ctx.reg_id
    )

    # 1) obligation decomposition for clauses that will run episodes (0–20%).
    obligations: dict[str, list[dict[str, Any]]] = (
        await _decompose_obligations(target, title, ctx.llm, ctx.progress) if target else {}
    )

    clauses = [
        ExternalClause(
            document_name=title,
            clause_number=r["clause_number"],
            section=r.get("section") or "",
            full_text=r["clause_text"],
            obligations=obligations.get(r["clause_number"], []),
            clause_type=r.get("clause_type") or "",
            internalization_decision=r.get("internalization_decision") or "",
            internalization_reason=r.get("internalization_reason") or "",
        )
        for r in rows
    ]

    # 2) episodes — one per clause; 无需内化 rows skip inside run_compare (20–90%).
    total = max(len(target), 1)

    def hook(clauses_done: int, _total: int) -> None:
        ctx.progress(0.2 + 0.7 * clauses_done / total, f"条款比对 {clauses_done}/{total}")

    result: CompareResult = await run_compare(
        clauses, store, ctx.llm,
        searcher=CONFIG_APP.searcher,
        persist_dir=str(ctx.reg_dir / "episodes"),
        model=ctx.model,
        provider=ctx.provider,
        progress_hook=hook,
    )

    reg_dir = ctx.reg_dir
    reg_dir.mkdir(parents=True, exist_ok=True)

    # 3) raw result to disk BEFORE the display-only relevance filter — the
    #    filter rewrites candidates in place and must not strip the json.
    (reg_dir / "compare_result.json").write_text(
        result.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 4) gap report (sync llm.invoke inside → thread), then filter, then review page.
    rep = await asyncio.to_thread(
        generate_gap_report, result, store, ctx.llm, output_dir=str(reg_dir)
    )
    if REVIEW_FILTER_RELEVANCE:
        await filter_relevant_candidates(result, ctx.llm, concurrency=COMPARE_CONCURRENCY)
    render_review_html(result, reg_dir / "内规检索复核.html", store_name="internal")

    # 5) matches ↔ rows are order-aligned by construction (one episode per row).
    for row, match in zip(rows, result.matches):
        await ctx.repos.clauses.update(row["_id"], _row_updates(match))

    summary = {
        "coverage_rate": round(result.coverage_rate, 4),
        "fully_covered_count": result.fully_covered_count,
        "partially_covered_count": result.partially_covered_count,
        "missing_count": result.internalizable_missing_count,
        "internalizable_count": result.internalizable_count,
        "no_internalization_count": result.no_internalization_count,
        "report_mode": rep["mode"],
    }
    logger.info(
        "analysis stage done for %s: coverage=%.1f%% mode=%s",
        ctx.reg_id, summary["coverage_rate"] * 100, rep["mode"],
    )
    return {"analysis_summary": summary, "analysis_index_signature": _index_signature(store)}
