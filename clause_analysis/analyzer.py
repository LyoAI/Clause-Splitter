"""Unified entry point: one clause in, one ``ClauseResult`` out.

Flow per clause:
    keywords (rules | llm) — always, 内规/附件 stop here when ``need_internalization=False``
    judge (text only) → 无需内化: return ·
                      → episode (ReAct over Zeus) → grounding → result

Stateless per call: the retrieval cache lives in a registry created for each
``analyze`` call. The analyzer is safe to share; concurrency is the caller's
choice (the intranet LLM allows two in flight).
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent import ReActAgent, ToolRegistry
from llm import get_llm
from rag import Chunk, RagAuthError, RagNotConfigured, RagService
from rag.providers.zeus import ZeusProvider
from utils import get_logger, segment_label

from . import config as cfg
from .episode import build_episode_prompt
from .grounding import Verdict, ground, resolve_ids
from .judge import Judgment, judge
from .keywords import extract_keywords, regex_keywords
from .prompts import load, render
from .types import (
    NO_INTERNALIZATION,
    ClauseInput,
    ClauseResult,
    CoverageStatus,
    RelatedTerm,
)

logger = get_logger()


class ClauseAnalyzer:
    def __init__(
        self,
        llm: Any,
        rag: RagService,
        *,
        timeout_s: float = cfg.EPISODE_TIMEOUT_S,
        max_tool_calls: int = cfg.MAX_TOOL_CALLS,
        top_k: int = cfg.SEARCH_TOP_K,
        snippet_max_tokens: int = cfg.SNIPPET_MAX_TOKENS,
    ) -> None:
        self.llm = llm
        self.rag = rag
        self.timeout_s = timeout_s
        self.max_tool_calls = max_tool_calls
        self.top_k = top_k
        self.snippet_max_tokens = snippet_max_tokens
        # Tool hints depend only on which tools exist, so one render serves all calls.
        # In llm keyword mode the episode answer additionally carries relation_keywords.
        probe = self._registry()
        hints = probe.tool_hints(cfg.PROMPT_LANG)
        self._system_prompts = {
            "rules": render("analysis_agent", tools=hints, relation_keywords_extra="", relation_keywords_rule=""),
            "llm": render(
                "analysis_agent",
                tools=hints,
                relation_keywords_extra=',"relation_keywords":{"<检索返回的 chunk_id>":"关键词1、关键词2"}',
                relation_keywords_rule="- `relation_keywords`:对 `matched_internal_clause_ids` 中的每条内规条款提取 3-6 个核心合规关键词,顿号分隔;",
            ),
        }
        self._force_answer = load("force_answer")

    @classmethod
    def from_config(cls) -> "ClauseAnalyzer":
        """Build the LLM and Zeus client from ``clause_analysis/config.py``."""
        llm = get_llm(
            cfg.LLM_MODEL_NAME, cfg.LLM_PROVIDER,
            config_dir=cfg.LLM_CONFIG_DIR, timeout_s=cfg.LLM_TIMEOUT_S,
        )
        rag = ZeusProvider(
            cfg.RAG_API_BASE, cfg.ZEUS_APP_ID, cfg.ZEUS_APP_SECRET, cfg.ZEUS_INDEX,
            timeout_s=cfg.RAG_TIMEOUT_S,
            kb_limit=cfg.ZEUS_KB_LIMIT,
            max_k=cfg.ZEUS_MAX_K,
            recall_min_score=cfg.ZEUS_RECALL_MIN_SCORE,
            rerank_min_score=cfg.ZEUS_RERANK_MIN_SCORE,
            max_concurrency=cfg.ZEUS_MAX_CONCURRENCY,
        )
        return cls(llm, rag)

    async def analyze(
        self,
        clause: ClauseInput,
        *,
        need_internalization: bool = True,
        keyword_mode: str = "rules",
        search_labels: list[str] | None = None,
    ) -> ClauseResult:
        """Extract keywords, then (外规 only) judge and compare against the corpus.

        Keywords are the前置 step and run for every clause — 内规/附件 need
        them too. ``need_internalization=False`` (docType 2/3) skips judgment
        and retrieval entirely: only identity fields + keywords are filled, and
        Zeus stays untouched. ``keyword_mode`` picks the extraction:
        ``"rules"`` (regex, default) or ``"llm"`` (falls back to rules on any
        failure).

        ``search_labels`` optionally narrows retrieval to Zeus chunks carrying
        those labels; ``None`` / empty searches the whole index. Retrieval scope
        is the caller's choice per call — never fixed at client init.

        Raises ``RagAuthError`` / ``RagNotConfigured`` for deployment problems so
        the caller can stop the batch. Every other failure degrades this clause only.
        """
        keyword = await extract_keywords(self.llm, clause, mode=keyword_mode, timeout_s=self.timeout_s)
        if not need_internalization:
            return _identity(clause, keyword)
        if not self.rag.available:
            raise RagNotConfigured("Zeus 未配置 —— 请设置 RAG_API_BASE / ZEUS_APP_ID / ZEUS_APP_SECRET / ZEUS_INDEX")

        judgment = await judge(self.llm, clause, self.timeout_s)
        if judgment.decision == NO_INTERNALIZATION:
            return _base(clause, judgment, keyword)
        registry = self._registry(search_labels)
        agent = ReActAgent(
            self.llm, registry,
            system_prompt=self._system_prompts["llm" if keyword_mode == "llm" else "rules"],
            force_answer_msg=self._force_answer,
            max_tool_calls=self.max_tool_calls,
        )
        try:
            episode = await asyncio.wait_for(
                agent.arun(build_episode_prompt(clause)), timeout=self.timeout_s
            )
            verdict = ground(episode.answer)
        except (RagAuthError, RagNotConfigured):
            raise
        except asyncio.TimeoutError:
            logger.warning("episode timeout (%.0fs): %s", self.timeout_s, clause.term_name)
            return _degraded(clause, judgment, keyword, f"该条款比对超时(>{self.timeout_s:g}s),未完成承接判定,需人工复核。")
        except Exception as e:  # noqa: BLE001 — one clause's failure must not stop the batch
            logger.error("episode failed %s: %s", clause.term_name, e)
            return _degraded(clause, judgment, keyword, "该条款比对执行失败,未完成承接判定,需人工复核。")

        # "No match" is only trustworthy when retrieval actually answered.
        if verdict.status == CoverageStatus.NOT_COVERED and registry.failed_searches:
            logger.warning("retrieval failed during %s; 缺失 is not trusted", clause.term_name)
            return _degraded(clause, judgment, keyword, "检索服务异常,未能完成承接判定,需人工复核。")

        return _result(clause, judgment, keyword, verdict, registry, keyword_mode)

    def _registry(self, search_labels: list[str] | None = None) -> ToolRegistry:
        return ToolRegistry(
            self.rag,
            top_k=self.top_k,
            snippet_max_tokens=self.snippet_max_tokens,
            default_labels=search_labels,
        )


def _identity(clause: ClauseInput, keyword: str) -> ClauseResult:
    """内规/附件: identity fields + keywords only, no internalization analysis."""
    return ClauseResult(
        term_name=clause.term_name,
        term_id=clause.term_id,
        term_content=clause.term_content,
        term_keyword=keyword,
        internalize="",
    )


def _base(clause: ClauseInput, judgment: Judgment, keyword: str) -> ClauseResult:
    """Judgment-only result (no analysis fields)."""
    return ClauseResult(
        term_name=clause.term_name,
        term_id=clause.term_id,
        term_content=clause.term_content,
        term_keyword=keyword,
        internalize=judgment.decision,
        internalize_reason=judgment.reason,
        require_type=judgment.require_type,
    )


def _degraded(clause: ClauseInput, judgment: Judgment, keyword: str, note: str) -> ClauseResult:
    """Failed episode: a 缺失 verdict whose analysis says it needs human review."""
    return ClauseResult(
        term_name=clause.term_name,
        term_id=clause.term_id,
        term_content=clause.term_content,
        term_keyword=keyword,
        internalize=judgment.decision,
        internalize_reason=judgment.reason,
        require_type=judgment.require_type,
        coverage_status=CoverageStatus.NOT_COVERED,
        analysis=note,
    )


def _relation_keyword(cid: str, chunk: Chunk | None, verdict: Verdict, mode: str) -> str:
    """Keywords for one adopted internal clause.

    ``llm`` mode prefers the episode-extracted keywords and falls back to the
    rules regex when the model gave none; ``rules`` mode always uses the regex.
    """
    if chunk is None:
        return ""
    if mode == "llm":
        kw = verdict.relation_keywords.get(cid, "")
        if kw:
            return kw
    return regex_keywords(chunk.text)


def _result(
    clause: ClauseInput, judgment: Judgment, keyword: str,
    verdict: Verdict, registry: ToolRegistry, keyword_mode: str,
) -> ClauseResult:
    related = [
        RelatedTerm(
            ai_relation_basis=verdict.evidence.get(cid, ""),
            # Zeus org_data_id = the document id recorded at ingestion (the backend docId).
            relation_doc_id=str(chunk.meta.get("org_data_id") or "") if chunk else "",
            term_content=chunk.text if chunk else "",
            term_id=cid,
            term_keyword=_relation_keyword(cid, chunk, verdict, keyword_mode),
            term_name=segment_label(chunk) if chunk else "",
        )
        for cid, chunk in resolve_ids(verdict.claimed_ids, registry)
    ]
    return ClauseResult(
        term_name=clause.term_name,
        term_id=clause.term_id,
        term_content=clause.term_content,
        term_keyword=keyword,
        internalize=judgment.decision,
        internalize_reason=judgment.reason,
        require_type=judgment.require_type,
        coverage_status=verdict.status,
        analysis=verdict.analysis,
        difference_analysis=verdict.difference_analysis,
        core_requirements=verdict.core_requirements,
        remedy_suggestion=verdict.remedy_suggestion,
        related_terms=related,
    )
