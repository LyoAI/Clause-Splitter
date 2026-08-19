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
from clause_splitter.agent.prompts import PRE_INTERNALIZE_PROMPT, build_pre_internalize_prompt
from clause_splitter.config import (
    COMPARE_CONCURRENCY,
    CONTEXT_SCOPE_MAX,
    EMBED_DEVICE,
    EMBED_MODEL,
    EPISODE_TIMEOUT_S,
    MAX_TOOL_CALLS,
    REVIEW_TOP_K,
    SEARCH_TOP_K,
    SNIPPET_MAX_TOKENS,
    doc_cache_dir,
)
from clause_splitter.models import (
    CompareResult,
    CoverageStatus,
    MatchResult,
    normalize_internalization_decision,
)
from clause_splitter.tools import ToolRegistry, build_searchers
from clause_splitter.tools.searcher.store import DocumentStore
from clause_splitter.utils import get_logger
from .episode import ExternalClause, run_clause_episode
from .ground import _norm_str, ground_episode, parse_answer_json
from .retrieve import pre_retrieve

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
    # 与索引构建同址:store 记录了自己落在哪个 cache root(自定义位置)。
    cache_root = doc_cache_dir(store.cache_key(), root=store.index_root or None)
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
        clause_type=ext.clause_type,
        internalization_decision=ext.internalization_decision,
        internalization_reason=ext.internalization_reason,
    )


def _no_internalize_match(ext: ExternalClause) -> MatchResult:
    """无需内化条款:不进入覆盖比对,直接生成轻量 MatchResult。

    覆盖率口径只统计需内化处理的条款,故此处不参与覆盖统计;复核页/报告
    靠 internalization_decision == "无需内化" 识别并排除出缺失/行动项。
    """
    return MatchResult(
        external_clause_id=ext.clause_id,
        external_document_name=ext.document_name,
        external_article_id=ext.clause_number,
        external_source_text=ext.full_text,
        status=CoverageStatus.NOT_COVERED,
        analysis="该条款为宣示性/程序性规定(或判定无需内化),不纳入覆盖比对。",
        remedy_suggestion="",
        clause_type=ext.clause_type,
        internalization_decision=ext.internalization_decision,
        internalization_reason=ext.internalization_reason,
    )


async def _pre_internalize(
    llm: Any, ext: ExternalClause, sem: asyncio.Semaphore, timeout_s: float
) -> None:
    """检索之前判定某条外规是否需内化,结果写入 ``ext``(决定是否进入 episode)。

    判定失败(超时/解析异常/LLM 报错)保持 fail-open:内化字段留空,按
    "需内化处理"保守进入 episode —— 不漏掉覆盖统计,也不阻塞整批比对。
    """
    async with sem:
        try:
            resp = await asyncio.wait_for(
                llm.ainvoke([
                    {"role": "system", "content": PRE_INTERNALIZE_PROMPT},
                    {"role": "user", "content": build_pre_internalize_prompt(ext)},
                ]),
                timeout=timeout_s,
            )
            content = resp.content if hasattr(resp, "content") else str(resp)
            parsed = parse_answer_json(content)
            ext.clause_type = _norm_str(parsed.get("clause_type") if parsed else "")
            # 两值化:预防性内化 → 需要内化(写入点归一,下游一致)。
            ext.internalization_decision = normalize_internalization_decision(
                _norm_str(parsed.get("internalization_decision") if parsed else "")
            )
            ext.internalization_reason = _norm_str(parsed.get("internalization_reason") if parsed else "")
        except Exception as e:  # noqa: BLE001 — 判定失败不拖垮整批
            logger.warning("前置内化判定失败(%s),按需内化处理: %s", ext.clause_id, e)


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
    review_top_k: int = REVIEW_TOP_K,
) -> CompareResult:
    """跑全部外规条款的比对 episode,聚合为 CompareResult。

    流程(内化判定前置于检索):
    1. 对每条外规先做**前置内化判定**(只依据条款原文,不检索内规库),
       决策写入 ``ExternalClause``(clause_type / internalization_decision / reason)。
    2. 无需内化条款 → 直接生成轻量 MatchResult,**不进入检索 episode**;
       需内化处理(需要内化/判定失败)条款 → 正常跑 episode 比对。
    3. 覆盖率口径只统计需内化处理的条款(见 ``CompareResult.coverage_rate``)。

    Args:
        external_clauses: 外规条款列表(每条 = 一个 episode)。
        store: 已建好索引的内规条款库。
        llm: ``LLM`` facade。
        searcher: grep / bm25 / faiss / hybrid(默认 hybrid 三工具全绑)。
        persist_dir: 非空则把每条 episode 轨迹落盘到该目录。
        model/provider/company: 落盘元数据。
        review_top_k: 每条外规条款附带展示的确定性批量预召回候选数
            (纯展示证据,不参与 episode;grounding 采用的条目打「✓ 采用」)。

    Returns:
        CompareResult;matches 与 external_clauses 一一对应(顺序一致)。
    """
    if not external_clauses:
        return CompareResult(
            external_documents=[], internal_documents=sorted(store.docs),
            total_external_obligations=0,
            total_internal_obligations=len(store.all_chunks()),
        )

    # 前置内化判定:检索之前逐条判定是否需内化(写入 ext)。
    pre_sem = asyncio.Semaphore(concurrency)
    await asyncio.gather(*(_pre_internalize(llm, c, pre_sem, episode_timeout) for c in external_clauses))
    n_skip = sum(1 for c in external_clauses if (c.internalization_decision or "").strip() == "无需内化")
    logger.info(
        "前置内化判定完成:%d 条外规条款,其中 %d 条无需内化(不进入覆盖比对)",
        len(external_clauses), n_skip,
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

    def _attach_candidates(match: MatchResult, ext: ExternalClause) -> None:
        """附上批量预召回候选(复核页展示用),并标记 grounding 采用的条目。"""
        try:
            candidates = pre_retrieve(
                store, list(registry.searchers.values()),
                ext.full_text, top_k=review_top_k,
            )
            adopted_ids = set(match.matched_internal_clause_ids or ())
            if not adopted_ids and match.matched_internal_id:
                adopted_ids = {match.matched_internal_id}
            for cand in candidates:
                cand.adopted = cand.chunk_id in adopted_ids
            match.retrieved_candidates = candidates
        except Exception as e:  # noqa: BLE001 — 展示数据失败不拖垮批量
            logger.warning("pre_retrieve 失败(%s),复核页该条无候选: %s", ext.clause_id, e)

    async def _run_one(ext: ExternalClause) -> MatchResult:
        # 无需内化条款:不检索、不跑 episode,直接给轻量结果(覆盖率不计)。
        if (ext.internalization_decision or "").strip() == "无需内化":
            return _no_internalize_match(ext)
        async with sem:
            try:
                result = await asyncio.wait_for(
                    run_clause_episode(agent, ext), timeout=episode_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("episode 超时(%.0fs): %s", episode_timeout, ext.clause_id)
                match = _timeout_match(ext, episode_timeout)
                _attach_candidates(match, ext)
                return match
            except Exception as e:  # noqa: BLE001 — 单条失败不拖垮整批
                logger.error("episode 失败 %s: %s", ext.clause_id, e)
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
            except Exception as e:  # noqa: BLE001 — grounding/校验失败降级为缺失,不拖垮整批
                logger.error("grounding 校验失败 %s: %s", ext.clause_id, e)
                match = MatchResult(
                    external_clause_id=ext.clause_id,
                    external_document_name=ext.document_name,
                    external_article_id=ext.clause_number,
                    external_source_text=ext.full_text,
                    status=CoverageStatus.NOT_COVERED,
                    analysis="该条款比对结果无法落地(grounding 校验失败),已降级为缺失,需人工复核。",
                    remedy_suggestion="待人工复核:确认该外规条款是否确有内规承接。",
                    clause_type=ext.clause_type,
                    internalization_decision=ext.internalization_decision,
                    internalization_reason=ext.internalization_reason,
                )
            _attach_candidates(match, ext)
            return match

    matches = await asyncio.gather(*(_run_one(c) for c in external_clauses))

    return CompareResult(
        external_documents=sorted({c.document_name for c in external_clauses}),
        internal_documents=sorted(store.docs),
        total_external_obligations=len(external_clauses),
        total_internal_obligations=len(store.all_chunks()),
        matches=[m for m in matches if m is not None],
    )
