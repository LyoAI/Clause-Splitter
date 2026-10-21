"""Batch orchestration — one episode per external clause, parallel, aggregated.

``run_compare()`` is the core comparison flow:

1. Engine assembly goes through ``orchestrator.build_registry`` /
   ``build_agent`` (chillrag backends + a ReAct agent with the
   analysis_agent.md prompt injected), built once and shared across episodes
   (``agent.arun`` rebuilds messages each round — no shared mutable state).
2. ``Semaphore(concurrency)`` + ``asyncio.wait_for(episode_timeout)`` run all
   clauses in parallel; a timed-out episode degrades to a
   ``terminated='timeout'`` MatchResult instead of failing the batch.
3. Each episode is persisted (``persist_result``) and grounded
   (``ground_episode`` → anti-hallucination ``MatchResult``).
4. Aggregation → ``CompareResult`` (fully/partially covered, missing, rates).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from chillrag.tools.searcher.store import DocumentStore

from clause_splitter.config import (
    COMPARE_CONCURRENCY,
    EPISODE_TIMEOUT_S,
    REVIEW_TOP_K,
)
from clause_splitter.models import (
    CompareResult,
    CoverageStatus,
    MatchResult,
    RetrievedCandidate,
)
from clause_splitter.utils import get_logger
from .episode import EpisodeResult, ExternalClause, run_clause_episode
from .ground import _segment_label, ground_episode
from .internalize import judge_internalization
from .persistence import persist_result
from .retrieve import pre_retrieve

logger = get_logger()


# ═══════════════════════════════════════════════════════════════════════
# Episode timeout degradation
# ═══════════════════════════════════════════════════════════════════════


def _timeout_match(ext: ExternalClause, timeout_s: float) -> MatchResult:
    """Timed-out episode → missing MatchResult (no invented adoption)."""
    return MatchResult(
        external_clause_id=ext.clause_id,
        external_document_name=ext.document_name,
        external_article_id=ext.clause_number,
        external_source_text=ext.full_text,
        status=CoverageStatus.NOT_COVERED,
        analysis=f"该条款比对超时(>{timeout_s:g}s),未完成承接判定,需人工复核。",
        clause_type=ext.clause_type,
        internalization_decision=ext.internalization_decision,
        internalization_reason=ext.internalization_reason,
    )


def _no_internalize_match(ext: ExternalClause) -> MatchResult:
    """No-internalization clauses skip coverage comparison entirely.

    Coverage rates only count clauses requiring internalization; the review
    page / report use internalization_decision == "无需内化" to keep these out
    of the missing/action lists.
    """
    return MatchResult(
        external_clause_id=ext.clause_id,
        external_document_name=ext.document_name,
        external_article_id=ext.clause_number,
        external_source_text=ext.full_text,
        status=CoverageStatus.NOT_COVERED,
        analysis="该条款为宣示性/程序性规定(或判定无需内化),不纳入覆盖比对。",
        clause_type=ext.clause_type,
        internalization_decision=ext.internalization_decision,
        internalization_reason=ext.internalization_reason,
    )


async def _pre_internalize(
    llm: Any, ext: ExternalClause, sem: asyncio.Semaphore, timeout_s: float
) -> None:
    """Judge whether a clause needs internalization before retrieval, into ``ext``.

    Fail-open: on judgment failure the fields stay empty and the clause
    conservatively enters the episode as "needs internalization" — nothing is
    dropped from coverage stats and the batch is never blocked.
    """
    async with sem:
        await judge_internalization(llm, ext, timeout_s)


# ═══════════════════════════════════════════════════════════════════════
# Comparison flow
# ═══════════════════════════════════════════════════════════════════════


def _ensure_adopted_in_candidates(
    candidates: list[RetrievedCandidate],
    adopted_ids: set[str],
    store: DocumentStore,
    query: str,
) -> None:
    """Force grounding-adopted clauses into the candidate list.

    Partial/full coverage must be visible on the review page even when the
    clause wasn't in the batch pre-recall top-k: injected candidates are
    marked adopted=True (kept unconditionally by the relevance filter),
    backend="episode", score=None. Works even when the recall list is empty
    (retrieval unavailable) so adopted clauses always show.
    """
    present = {c.chunk_id for c in candidates}
    for cid in adopted_ids:
        if cid in present:
            continue
        chunk = store.get_chunk(cid)
        if chunk is None:
            continue
        candidates.append(RetrievedCandidate(
            chunk_id=cid,
            doc_name=chunk.doc_id,
            clause_label=_segment_label(chunk),
            backend="episode",
            score=None,
            query=query,
            snippet=(chunk.text or "")[:180],
            full_text=chunk.text or "",
            adopted=True,
        ))


async def run_compare(
    external_clauses: list[ExternalClause],
    store: DocumentStore,
    llm: Any,
    *,
    searcher: str = "hybrid",
    concurrency: int = COMPARE_CONCURRENCY,
    episode_timeout: float = EPISODE_TIMEOUT_S,
    persist_dir: str | None = None,
    model: str = "",
    provider: str = "",
    company: str = "",
    review_top_k: int = REVIEW_TOP_K,
    progress_hook: Callable[[int, int], None] | None = None,
) -> CompareResult:
    """Run comparison episodes for all external clauses → CompareResult.

    Flow (internalization judgment precedes retrieval):
    1. Each clause first gets a **pre-internalization judgment** (clause text
       only, no retrieval), written into ``ExternalClause``
       (clause_type / internalization_decision / reason).
    2. "无需内化" clauses → lightweight MatchResult, no retrieval episode;
       clauses needing internalization (or with a failed judgment) run the
       full episode.
    3. Coverage rates only count clauses requiring internalization
       (see ``CompareResult.coverage_rate``).

    Args:
        external_clauses: External clauses (one episode each).
        store: Internal clause library with indexes built.
        llm: ``LLM`` facade.
        searcher: grep / bm25 / faiss / hybrid (default hybrid).
        persist_dir: When set, persist each episode trajectory there.
        model/provider/company: Persistence metadata.
        review_top_k: Deterministic batch pre-recall candidates attached per
            clause for the review page (display-only; grounding-adopted ones
            get the "✓ adopted" mark).
        progress_hook: Optional ``(clauses_done, total)`` callback fired once
            per finished clause (episode / no-internalization / degraded);
            failures only warn.

    Returns:
        CompareResult; matches line up with external_clauses (same order).
    """
    if not external_clauses:
        return CompareResult(
            external_documents=[], internal_documents=sorted(store.docs),
            total_external_obligations=0,
            total_internal_obligations=len(store.all_chunks()),
        )

    # Pre-internalization judgment before retrieval. Clauses already judged
    # (pipeline stage 3 wrote the fields) are reused — no double billing.
    to_judge = [c for c in external_clauses if not (c.internalization_decision or "").strip()]
    pre_sem = asyncio.Semaphore(concurrency)
    await asyncio.gather(*(_pre_internalize(llm, c, pre_sem, episode_timeout) for c in to_judge))
    n_skip = sum(1 for c in external_clauses if (c.internalization_decision or "").strip() == "无需内化")
    logger.info(
        "pre-internalization done: %d clauses, %d need no internalization (excluded from coverage)",
        len(external_clauses), n_skip,
    )

    # Lazy import: orchestrator -> pipeline -> aggregate closes the import
    # cycle; resolving at call time also keeps this monkeypatch-friendly.
    from clause_splitter.orchestrator import build_agent, build_registry

    registry = build_registry(store, searcher)
    agent = build_agent(llm, registry)
    sem = asyncio.Semaphore(concurrency)

    def _attach_candidates(match: MatchResult, ext: ExternalClause) -> None:
        """Attach batch pre-recall candidates (review page) + force-adopted ones."""
        candidates: list[RetrievedCandidate] = []
        try:
            candidates = pre_retrieve(
                store, list(registry.searchers.values()),
                ext.full_text, top_k=review_top_k,
            )
        except Exception as e:  # noqa: BLE001 — display data must not fail the batch
            logger.warning("pre_retrieve failed (%s); no candidates for this clause: %s",
                           ext.clause_id, e)
        adopted_ids = set(match.matched_internal_clause_ids or ())
        if not adopted_ids and match.matched_internal_id:
            adopted_ids = {match.matched_internal_id}
        for cand in candidates:
            cand.adopted = cand.chunk_id in adopted_ids
        _ensure_adopted_in_candidates(candidates, adopted_ids, store, ext.full_text)
        match.retrieved_candidates = candidates

    async def _run_one(ext: ExternalClause) -> MatchResult:
        # No-internalization clause: no retrieval, no episode — lightweight result.
        if (ext.internalization_decision or "").strip() == "无需内化":
            return _no_internalize_match(ext)
        async with sem:
            try:
                result: EpisodeResult = await asyncio.wait_for(
                    run_clause_episode(agent, ext), timeout=episode_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("episode timeout (%.0fs): %s", episode_timeout, ext.clause_id)
                match = _timeout_match(ext, episode_timeout)
                _attach_candidates(match, ext)
                return match
            except Exception as e:  # noqa: BLE001 — one failure must not fail the batch
                logger.error("episode failed %s: %s", ext.clause_id, e)
                match = _timeout_match(ext, episode_timeout)
                _attach_candidates(match, ext)
                return match

            if persist_dir:
                persist_result(
                    result, persist_dir,
                    model=model, provider=provider, company=company,
                    searcher=searcher, query_id=ext.clause_id,
                )
            try:
                match = MatchResult(**ground_episode(result, store, ext))
            except Exception as e:  # noqa: BLE001 — grounding failure degrades to missing
                logger.error("grounding failed %s: %s", ext.clause_id, e)
                match = MatchResult(
                    external_clause_id=ext.clause_id,
                    external_document_name=ext.document_name,
                    external_article_id=ext.clause_number,
                    external_source_text=ext.full_text,
                    status=CoverageStatus.NOT_COVERED,
                    analysis="该条款比对结果无法落地(grounding 校验失败),已降级为缺失,需人工复核。",
                    clause_type=ext.clause_type,
                    internalization_decision=ext.internalization_decision,
                    internalization_reason=ext.internalization_reason,
                )
            _attach_candidates(match, ext)
            return match

    done = 0

    async def _run_one_counted(ext: ExternalClause) -> MatchResult:
        """_run_one + progress: one callback per finished clause."""
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
        internal_documents=sorted(store.docs),
        total_external_obligations=len(external_clauses),
        total_internal_obligations=len(store.all_chunks()),
        matches=[m for m in matches if m is not None],
    )
