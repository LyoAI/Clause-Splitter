"""Unified entry point: one external clause in, one ``ClauseResult`` out.

Flow per clause:
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
from rag import RagAuthError, RagNotConfigured, RagService
from rag.providers.zeus import ZeusProvider
from utils import get_logger, segment_label

from . import config as cfg
from .episode import build_episode_prompt
from .grounding import Verdict, ground, resolve_ids
from .judge import Judgment, judge
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
        probe = self._registry()
        self._system_prompt = render("analysis_agent", tools=probe.tool_hints(cfg.PROMPT_LANG))
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
            search_labels=cfg.ZEUS_SEARCH_LABELS,
        )
        return cls(llm, rag)

    async def analyze(self, clause: ClauseInput) -> ClauseResult:
        """Judge, then (if needed) compare one clause against the internal corpus.

        Raises ``RagAuthError`` / ``RagNotConfigured`` for deployment problems so
        the caller can stop the batch. Every other failure degrades this clause only.
        """
        if not self.rag.available:
            raise RagNotConfigured("Zeus 未配置 —— 请设置 RAG_API_BASE / ZEUS_APP_ID / ZEUS_APP_SECRET / ZEUS_INDEX")

        judgment = await judge(self.llm, clause, self.timeout_s)
        if judgment.decision == NO_INTERNALIZATION:
            return _base(clause, judgment)

        registry = self._registry()
        agent = ReActAgent(
            self.llm, registry,
            system_prompt=self._system_prompt,
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
            logger.warning("episode timeout (%.0fs): %s", self.timeout_s, clause.clause_number)
            return _degraded(clause, judgment, f"该条款比对超时(>{self.timeout_s:g}s),未完成承接判定,需人工复核。")
        except Exception as e:  # noqa: BLE001 — one clause's failure must not stop the batch
            logger.error("episode failed %s: %s", clause.clause_number, e)
            return _degraded(clause, judgment, "该条款比对执行失败,未完成承接判定,需人工复核。")

        # "No match" is only trustworthy when retrieval actually answered.
        if verdict.status == CoverageStatus.NOT_COVERED and registry.failed_searches:
            logger.warning("retrieval failed during %s; 缺失 is not trusted", clause.clause_number)
            return _degraded(clause, judgment, "检索服务异常,未能完成承接判定,需人工复核。")

        return _result(clause, judgment, verdict, registry)

    def _registry(self) -> ToolRegistry:
        return ToolRegistry(self.rag, top_k=self.top_k, snippet_max_tokens=self.snippet_max_tokens)


def _base(clause: ClauseInput, judgment: Judgment) -> ClauseResult:
    """Judgment-only result (no analysis fields)."""
    return ClauseResult(
        clause_number=clause.clause_number,
        internalize=judgment.decision,
        internalize_reason=judgment.reason,
        require_type=judgment.require_type,
    )


def _degraded(clause: ClauseInput, judgment: Judgment, note: str) -> ClauseResult:
    """Failed episode: a 缺失 verdict whose analysis says it needs human review."""
    return ClauseResult(
        clause_number=clause.clause_number,
        internalize=judgment.decision,
        internalize_reason=judgment.reason,
        require_type=judgment.require_type,
        coverage_status=CoverageStatus.NOT_COVERED,
        analysis=note,
    )


def _result(
    clause: ClauseInput, judgment: Judgment, verdict: Verdict, registry: ToolRegistry
) -> ClauseResult:
    related = [
        RelatedTerm(
            ai_relation_basis=verdict.evidence.get(cid, ""),
            # Zeus org_data_id = the document id recorded at ingestion (the backend docId).
            relation_doc_id=str(chunk.meta.get("org_data_id") or "") if chunk else "",
            term_content=chunk.text if chunk else "",
            term_id=cid,
            term_name=segment_label(chunk) if chunk else "",
        )
        for cid, chunk in resolve_ids(verdict.claimed_ids, registry)
    ]
    return ClauseResult(
        clause_number=clause.clause_number,
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
