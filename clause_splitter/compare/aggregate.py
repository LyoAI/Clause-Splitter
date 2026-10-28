"""Batch comparison — one episode per external clause, parallel, aggregated."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from clause_splitter.config import COMPARE_CONCURRENCY, EPISODE_TIMEOUT_S, REVIEW_TOP_K
from clause_splitter.models import (
    CompareResult,
    CoverageStatus,
    MatchResult,
    RetrievedCandidate,
)
from clause_splitter.rag import (
    RagAuthError,
    RagError,
    RagNotConfigured,
    RagService,
    RagUnavailable,
)
from clause_splitter.utils import get_logger, segment_label

from .episode import ExternalClause, run_clause_episode
from .ground import ground_episode
from .internalize import judge_internalization
from .persistence import persist_result
from .retrieve import pre_retrieve

logger = get_logger()


def _fallback_match(ext: ExternalClause, analysis: str) -> MatchResult:
    """Missing MatchResult for a clause that could not be compared."""
    return MatchResult(
        external_clause_id=ext.clause_id,
        external_document_name=ext.document_name,
        external_article_id=ext.clause_number,
        external_source_text=ext.full_text,
        status=CoverageStatus.NOT_COVERED,
        analysis=analysis,
        clause_type=ext.clause_type,
        internalization_decision=ext.internalization_decision,
        internalization_reason=ext.internalization_reason,
    )


def _ensure_adopted_in_candidates(
    candidates: list[RetrievedCandidate],
    adopted_ids: set[str],
    lookup: Any,
    query: str,
) -> None:
    """Force grounding-adopted clauses into the review-page candidates."""
    present = {c.chunk_id for c in candidates}
    for cid in adopted_ids:
        if cid in present:
            continue
        chunk = lookup.get_chunk(cid)
        if chunk is None:
            continue
        candidates.append(RetrievedCandidate(
            chunk_id=cid,
            doc_name=chunk.doc_id,
            clause_label=segment_label(chunk),
            backend="episode",
            score=None,
            query=query,
            snippet=(chunk.text or "")[:180],
            full_text=chunk.text or "",
            adopted=True,
        ))


def _corpus_view(service: RagService) -> tuple[list[str], int]:
    """Document names + chunk count for the review page; failures degrade to empty.

    Providers with no inventory endpoint (Zeus) report unknown instead of
    paying for calls that cannot answer.
    """
    if not getattr(service, "supports_document_listing", True):
        return [], 0
    docs: list[str] = []
    chunks = 0
    try:
        docs = sorted({d.name or d.doc_id for d in service.documents()})
    except RagError as e:
        logger.warning("RAG documents() failed: %s", e)
    try:
        chunks = service.stats().chunk_count
    except RagError as e:
        logger.warning("RAG stats() failed: %s", e)
    return docs, chunks


async def _probe_retrieval(service: RagService) -> None:
    """Fail the stage when retrieval is unreachable, before any episode runs.

    A transient 5xx deep inside an episode is deliberately degraded to a
    per-clause "需人工复核"; that is fine for one clause and wrong for the whole
    run. This probe distinguishes "retrieval is down" from "nothing matched":
    only availability errors abort, an empty result or a slow single query does
    not.
    """
    try:
        await asyncio.to_thread(service.search, "数据安全", 1)
    except RagUnavailable as exc:
        raise RagUnavailable(
            f"内规检索不可用，分析阶段中止（{exc}）—— 请检查 RAG 服务状态后重跑"
        ) from exc
    except RagError as exc:
        # Bad credentials, missing configuration or a contract mismatch: also a
        # deployment problem, never "no hits".
        logger.error("retrieval probe failed: %s", exc)
        raise
    logger.info("retrieval probe ok: %s", service.name)


async def run_compare(
    external_clauses: list[ExternalClause],
    service: RagService,
    llm: Any,
    *,
    concurrency: int = COMPARE_CONCURRENCY,
    episode_timeout: float = EPISODE_TIMEOUT_S,
    persist_dir: str | None = None,
    review_top_k: int = REVIEW_TOP_K,
    progress_hook: Callable[[int, int], None] | None = None,
) -> CompareResult:
    """Compare every external clause and aggregate the matches.

    Clauses are first judged for internalization (text only, no retrieval);
    "无需内化" ones get a lightweight result and do not count toward coverage.
    The rest run one ReAct episode each, then grounding verifies every claimed
    internal chunk id. One episode's timeout or failure degrades only itself.
    """
    internal_documents, internal_chunks = await asyncio.to_thread(_corpus_view, service)
    if not external_clauses:
        return CompareResult(
            external_documents=[],
            internal_documents=internal_documents,
            total_external_obligations=0,
            total_internal_obligations=internal_chunks,
        )

    # Probe retrieval once before spending LLM calls per clause. A 5xx here
    # would otherwise turn into "需人工复核" on every clause and still report a
    # successful stage — a fake all-missing report.
    await _probe_retrieval(service)

    to_judge = [c for c in external_clauses if not (c.internalization_decision or "").strip()]
    pre_sem = asyncio.Semaphore(concurrency)

    async def _judge(clause: ExternalClause) -> None:
        async with pre_sem:
            await judge_internalization(llm, clause, episode_timeout)

    await asyncio.gather(*(_judge(c) for c in to_judge))
    n_skip = sum(1 for c in external_clauses if (c.internalization_decision or "").strip() == "无需内化")
    logger.info(
        "pre-internalization done: %d clauses, %d need no internalization (excluded from coverage)",
        len(external_clauses), n_skip,
    )

    # Lazy import closes the orchestrator -> pipeline -> aggregate cycle.
    from clause_splitter.orchestrator import build_agent, build_registry

    registry = build_registry(service)
    agent = build_agent(llm, registry)
    sem = asyncio.Semaphore(concurrency)

    async def _attach_candidates(match: MatchResult, ext: ExternalClause) -> None:
        """Attach batch pre-recall candidates (review page) + force-adopted ones."""
        candidates: list[RetrievedCandidate] = []
        try:
            candidates = await asyncio.to_thread(
                pre_retrieve, service, ext.full_text, top_k=review_top_k
            )
        except Exception as e:  # noqa: BLE001 — display data must not fail the batch
            logger.warning("pre_retrieve failed (%s); no candidates for this clause: %s",
                           ext.clause_id, e)
        adopted_ids = set(match.matched_internal_clause_ids or ())
        if not adopted_ids and match.matched_internal_id:
            adopted_ids = {match.matched_internal_id}
        for cand in candidates:
            cand.adopted = cand.chunk_id in adopted_ids
        _ensure_adopted_in_candidates(candidates, adopted_ids, registry, ext.full_text)
        match.retrieved_candidates = candidates

    async def _run_one(ext: ExternalClause) -> MatchResult:
        if (ext.internalization_decision or "").strip() == "无需内化":
            return _fallback_match(
                ext, "该条款为宣示性/程序性规定(或判定无需内化),不纳入覆盖比对。"
            )
        async with sem:
            try:
                result = await asyncio.wait_for(
                    run_clause_episode(agent, ext), timeout=episode_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("episode timeout (%.0fs): %s", episode_timeout, ext.clause_id)
                match = _fallback_match(
                    ext,
                    f"该条款比对超时(>{episode_timeout:g}s),未完成承接判定,需人工复核。",
                )
                await _attach_candidates(match, ext)
                return match
            except (RagAuthError, RagNotConfigured):
                # Credential/configuration problems are deployment errors: fail
                # the stage instead of emitting an all-missing report.
                raise
            except Exception as e:  # noqa: BLE001 — one failure must not fail the batch
                logger.error("episode failed %s: %s", ext.clause_id, e)
                match = _fallback_match(
                    ext, "该条款比对执行失败,未完成承接判定,需人工复核。"
                )
                await _attach_candidates(match, ext)
                return match

            if persist_dir:
                persist_result(
                    result, persist_dir, searcher=service.name, query_id=ext.clause_id,
                )
            try:
                match = ground_episode(result, registry, ext)
            except Exception as e:  # noqa: BLE001 — grounding failure degrades to missing
                logger.error("grounding failed %s: %s", ext.clause_id, e)
                match = _fallback_match(
                    ext,
                    "该条款比对结果无法落地(grounding 校验失败),已降级为缺失,需人工复核。",
                )
            await _attach_candidates(match, ext)
            return match

    done = 0

    async def _run_one_counted(ext: ExternalClause) -> MatchResult:
        nonlocal done
        match = await _run_one(ext)
        done += 1
        if progress_hook is not None:
            try:
                progress_hook(done, len(external_clauses))
            except Exception as e:  # noqa: BLE001 — progress must not fail the batch
                logger.warning("progress_hook failed: %s", e)
        return match

    matches = await asyncio.gather(*(_run_one_counted(c) for c in external_clauses))

    return CompareResult(
        external_documents=sorted({c.document_name for c in external_clauses}),
        internal_documents=internal_documents,
        total_external_obligations=len(external_clauses),
        total_internal_obligations=internal_chunks,
        matches=[m for m in matches if m is not None],
    )
