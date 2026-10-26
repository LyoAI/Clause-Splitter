"""Core orchestrator — the single meeting point of engine wiring and pipeline.

1. **Engine wiring (seam)**: the tool registry and ReAct agent are assembled
   here over the external RAG service. Business code depends only on these
   products; swapping to the internal team's service only changes
   ``clause_splitter/rag/client.py``.
2. **Chain orchestration**: applicability → split → internalization → analysis
   in one job, with the manual-push skip for stage 1 and per-stage progress
   weighting.
"""

from __future__ import annotations

from typing import Any

from clause_splitter.agent import ReActAgent, ToolRegistry
from clause_splitter.config import (
    CONTEXT_SCOPE_MAX,
    MAX_TOOL_CALLS,
    PROMPT_LANG,
    SEARCH_TOP_K,
    SNIPPET_MAX_TOKENS,
)
from clause_splitter.pipeline import applicability, analysis, doc_profile, internalize, split_stage
from clause_splitter.pipeline.contexts import DERIVED_FIELDS, STAGES, StageContext, StageError
from clause_splitter.prompts import load, render
from clause_splitter.rag import RagService, get_rag_service
from clause_splitter.utils import get_logger

logger = get_logger()


# ═══════════════════════════════════════════════════════════════════════
# Engine wiring
# ═══════════════════════════════════════════════════════════════════════


def build_registry(service: RagService | None = None) -> ToolRegistry:
    """RAG service → tool registry (search + optional context/materials tools)."""
    registry = ToolRegistry(
        service or get_rag_service(),
        top_k=SEARCH_TOP_K,
        snippet_max_tokens=SNIPPET_MAX_TOKENS,
        context_scope_max=CONTEXT_SCOPE_MAX,
    )
    logger.info("bound tools: %s", ", ".join(registry.tool_names()))
    return registry


def build_agent(llm: Any, registry: ToolRegistry) -> ReActAgent:
    """The compare ReActAgent: system prompt = prompts/analysis_agent.md (with
    the tool list injected), force-answer = prompts/force_answer.md, per-episode
    tool budget = MAX_TOOL_CALLS."""
    return ReActAgent(
        llm, registry,
        max_tool_calls=MAX_TOOL_CALLS,
        system_prompt=render("analysis_agent", tools=registry.tool_hints(PROMPT_LANG)),
        force_answer_msg=load("force_answer"),
    )


# ═══════════════════════════════════════════════════════════════════════
# Chain orchestration (all four stages in one job)
# ═══════════════════════════════════════════════════════════════════════

#: Chain progress weights per stage (sum = 1.0).
WEIGHTS = {"applicability": 0.10, "split": 0.15, "internalization": 0.25, "analysis": 0.50}


async def run_chain(ctx: StageContext) -> dict[str, Any]:
    """Applicable ⇒ split → internalization → analysis; otherwise downstream
    stages are marked ``skipped`` (per-stage endpoints can still force them)."""
    regs = ctx.repos.regulations
    raw_progress = ctx.progress
    offset = 0.0

    def bind(name: str) -> None:
        weight = WEIGHTS[name]

        def mapper(local: float, step: str, _w: float = weight) -> None:
            raw_progress(offset + _w * min(max(local, 0.0), 1.0), step)

        ctx.progress = mapper

    async def run_stage(name: str, fn, **kwargs: Any) -> dict[str, Any]:
        nonlocal offset
        bind(name)
        await regs.update(ctx.reg_id, {f"{name}_state": "running"})
        try:
            out = await fn(ctx, **kwargs) or {}
        except Exception:
            await regs.update(ctx.reg_id, {f"{name}_state": "failed"})
            raise
        await regs.update(ctx.reg_id, {f"{name}_state": "done", **out})
        offset += WEIGHTS[name]
        raw_progress(offset, f"{name} done")
        return out

    # Manual backend pushes (OptType=手动) are applicable by definition — skip
    # the stage-1 LLM call and stamp the override directly.
    if ctx.payload.get("skip_applicability"):
        await regs.update(ctx.reg_id, {
            "applicability_applicable": True,
            "applicability_confidence": 1.0,
            "applicability_reason": "手动推送(OptType=手动),默认适用于平安科技",
            "applicability_state": "done",
        })
        offset = WEIGHTS["applicability"]
        raw_progress(offset, "applicability skipped (manual push)")
        applicable = True
    else:
        app = await run_stage("applicability", applicability.run)
        applicable = app.get("applicability_applicable")

    if applicable is not True:
        reason = "法规不适用于本公司" if applicable is False else "适用性待人工复核"
        fields: dict[str, Any] = {"chain_skip_reason": reason}
        for name in STAGES[1:]:
            fields[f"{name}_state"] = "skipped"
            fields.update(DERIVED_FIELDS.get(name, {}))
        await regs.update(ctx.reg_id, fields)
        # The cleared DERIVED_FIELDS["split"] above wipes any profile, so build
        # it only after that write.
        raw_progress(offset, "生成文档摘要")
        await doc_profile.ensure(ctx)
        logger.info("chain stopped after stage 1 for %s: %s", ctx.reg_id, reason)
        return {"chain_stopped": reason}

    if not ctx.rag.available:
        raise StageError("RAG 服务未配置 —— 请设置 RAG_API_BASE 后重启服务")

    await run_stage("split", split_stage.run)
    await run_stage("internalization", internalize.run)
    await run_stage("analysis", analysis.run)
    return {"chain_completed": True}
