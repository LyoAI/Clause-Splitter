"""batch 编排 —— 每条外规条款一个 episode,并行执行,聚合为 CompareResult.

``run_compare()`` 是比对的核心编排,流程:

1. 按 ``searcher`` 类型构建检索后端(hybrid = grep+bm25+faiss 全绑,LLM 每轮自选)。
2. ``ToolRegistry`` / ``ReActAgent`` 各建一次,跨 episode 共享
   (``agent.arun`` 每轮重建 messages,无共享可变状态,可安全复用)。
3. ``Semaphore(concurrency)`` + ``asyncio.wait_for(episode_timeout)`` 并行跑全部条款;
   单条 episode 超时落 ``terminated='timeout'`` 的降级 MatchResult,不拖垮整批。
4. 每条 episode ``persist_result`` 落盘 + ``ground_episode`` → ``MatchResult``(防幻觉)。
5. 聚合 → ``CompareResult``(完全覆盖/部分覆盖/缺失/覆盖率统计)。
"""

from __future__ import annotations

import asyncio
from typing import Any

from clause_splitter.agent import ReActAgent, persist_result
from clause_splitter.config import (
    COMPARE_CONCURRENCY,
    CONTEXT_SCOPE_MAX,
    EMBED_DEVICE,
    EMBED_MODEL,
    EPISODE_TIMEOUT_S,
    MAX_TOOL_CALLS,
    SEARCH_TOP_K,
    SNIPPET_MAX_TOKENS,
    doc_cache_dir,
)
from clause_splitter.models import CompareResult, CoverageStatus, MatchResult
from clause_splitter.tools import ToolRegistry, build_searchers
from clause_splitter.tools.searcher.store import DocumentStore
from clause_splitter.utils import get_logger
from .episode import ExternalClause, run_clause_episode
from .ground import ground_episode, parse_answer_json

logger = get_logger()


# ═══════════════════════════════════════════════════════════════════════
# 检索后端装配
# ═══════════════════════════════════════════════════════════════════════


def _build_registry(
    searcher: str,
    store: DocumentStore,
    *,
    top_k: int,
    snippet_max_tokens: int,
    context_scope_max: int,
) -> tuple[ToolRegistry, list[str]]:
    """按 searcher 类型构建 ToolRegistry;返回 (registry, 实际绑定工具名列表)。

    faiss 后端依赖 embedding 模型 + 持久化索引;两者任一不可用时降级跳过,
    不阻塞 hybrid(grep+bm25 仍可用)。
    """
    cache_root = doc_cache_dir(store.cache_key())
    chunks_dir = str(cache_root / "chunks")
    faiss_dir = cache_root / "faiss"

    kwargs: dict[str, Any] = {}
    if searcher in ("grep", "hybrid"):
        kwargs["rg_dir"] = chunks_dir
    if searcher in ("faiss", "hybrid"):
        from clause_splitter.tools.searcher.embed import Embedder

        embedder = Embedder(model_name=EMBED_MODEL, device=EMBED_DEVICE)
        kwargs["embed_query"] = embedder.encode_query
        kwargs["faiss_index_dir"] = str(faiss_dir) if faiss_dir.is_dir() else None

    try:
        searchers = build_searchers(searcher, store, **kwargs)
    except Exception as e:  # noqa: BLE001 — faiss 不可用时降级
        logger.warning("检索装配失败(%s),回退为 bm25: %s", searcher, e)
        searchers = build_searchers("bm25", store)

    registry = ToolRegistry(
        searchers,
        store,
        top_k=top_k,
        snippet_max_tokens=snippet_max_tokens,
        context_scope_max=context_scope_max,
    )
    return registry, registry.tool_names()


# ═══════════════════════════════════════════════════════════════════════
# episode 超时降级
# ═══════════════════════════════════════════════════════════════════════


def _timeout_match(ext: ExternalClause, timeout_s: float) -> MatchResult:
    """episode 超时 → 缺失态 MatchResult(不编造承接,明确标注超时)。"""
    return MatchResult(
        external_clause_id=ext.clause_id,
        external_document_name=ext.document_name,
        external_article_id=ext.clause_number,
        external_source_text=ext.full_text,
        status=CoverageStatus.NOT_COVERED,
        analysis=f"该条款比对超时(>{timeout_s:g}s),未完成承接判定,需人工复核。",
        remedy_suggestion="待人工复核:确认该外规条款是否确有内规承接。",
    )


# ═══════════════════════════════════════════════════════════════════════
# 比对编排
# ═══════════════════════════════════════════════════════════════════════


async def run_compare(
    external_clauses: list[ExternalClause],
    store: DocumentStore,
    llm: Any,
    *,
    searcher: str = "hybrid",
    top_k: int = SEARCH_TOP_K,
    snippet_max_tokens: int = SNIPPET_MAX_TOKENS,
    context_scope_max: int = CONTEXT_SCOPE_MAX,
    max_tool_calls: int = MAX_TOOL_CALLS,
    concurrency: int = COMPARE_CONCURRENCY,
    episode_timeout: float = EPISODE_TIMEOUT_S,
    persist_dir: str | None = None,
    model: str = "",
    provider: str = "",
    company: str = "",
) -> CompareResult:
    """跑全部外规条款的比对 episode,聚合为 CompareResult。

    Args:
        external_clauses: 外规条款列表(每条 = 一个 episode)。
        store: 已建好索引的内规条款库。
        llm: ``LLM`` facade。
        searcher: grep / bm25 / faiss / hybrid(默认 hybrid 三工具全绑)。
        persist_dir: 非空则把每条 episode 轨迹落盘到该目录。
        model/provider/company: 落盘元数据。

    Returns:
        CompareResult;matches 与 external_clauses 一一对应(顺序一致)。
    """
    if not external_clauses:
        return CompareResult(
            external_documents=[], internal_documents=sorted(store.docs),
            total_external_obligations=0,
            total_internal_obligations=len(store.all_chunks()),
        )

    registry, bound_tools = _build_registry(
        searcher, store,
        top_k=top_k, snippet_max_tokens=snippet_max_tokens,
        context_scope_max=context_scope_max,
    )
    logger.info("绑定工具: %s", ", ".join(bound_tools))

    agent = ReActAgent(
        llm, registry,
        max_tool_calls=max_tool_calls,
        answer_parser=parse_answer_json,
    )
    sem = asyncio.Semaphore(concurrency)

    async def _run_one(ext: ExternalClause) -> MatchResult:
        async with sem:
            try:
                result = await asyncio.wait_for(
                    run_clause_episode(agent, ext), timeout=episode_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("episode 超时(%.0fs): %s", episode_timeout, ext.clause_id)
                return _timeout_match(ext, episode_timeout)
            except Exception as e:  # noqa: BLE001 — 单条失败不拖垮整批
                logger.error("episode 失败 %s: %s", ext.clause_id, e)
                return _timeout_match(ext, episode_timeout)

            if persist_dir:
                persist_result(
                    result, persist_dir,
                    model=model, provider=provider, company=company,
                    searcher=searcher, query_id=ext.clause_id,
                )
            return MatchResult(**ground_episode(result, store, ext))

    matches = await asyncio.gather(*(_run_one(c) for c in external_clauses))

    return CompareResult(
        external_documents=sorted({c.document_name for c in external_clauses}),
        internal_documents=sorted(store.docs),
        total_external_obligations=len(external_clauses),
        total_internal_obligations=len(store.all_chunks()),
        matches=[m for m in matches if m is not None],
    )
