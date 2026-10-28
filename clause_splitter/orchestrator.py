"""Core orchestrator — the single meeting point of engine wiring and pipeline.

1. **Engine wiring (seam)**: the tool registry and ReAct agent are assembled
   here over the external RAG service. Business code depends only on
   ``RagService``; swapping providers only changes
   ``clause_splitter/rag/providers/`` and ``RAG_PROVIDER``.
2. **Chain orchestration**: applicability → split → internalization → analysis
   in one job, with the manual-push skip for stage 1 and per-stage progress
   weighting.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
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
from clause_splitter.pipeline.contexts import (
    DERIVED_FIELDS,
    STAGES,
    StageContext,
    StageError,
    is_internal,
)
from clause_splitter.prompts import load, render
from clause_splitter.rag import RagError, RagService, get_rag_service
from clause_splitter.utils import get_logger

logger = get_logger()


def build_registry(service: RagService | None = None) -> ToolRegistry:
    """RAG service → tool registry (search / context / materials)."""
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


#: Chain progress weights per stage (sum = 1.0).
WEIGHTS = {"applicability": 0.10, "split": 0.15, "internalization": 0.25, "analysis": 0.50}

#: Internal/attachment chain: split dominates (it is the deliverable), the rest
#: is the corpus upload.
INTERNAL_WEIGHTS = {"split": 0.70, "ingest": 0.30}


def _stage_metrics(name: str, out: dict[str, Any]) -> str:
    """One-line stage result for the log — the numbers an operator looks for."""
    if name == "applicability":
        applicable = out.get("applicability_applicable")
        verdict = "适用" if applicable is True else "不适用" if applicable is False else "待人工复核"
        return f"判定={verdict} 置信度={out.get('applicability_confidence', 0)}"
    if name == "split":
        return f"条款={out.get('article_count', 0)} 模式={out.get('split_mode') or '-'}"
    if name == "internalization":
        return (
            f"需内化={out.get('needs_internalization_count', 0)}"
            f"/{out.get('clause_count', 0)}"
        )
    if name == "analysis":
        summary = out.get("analysis_summary") or {}
        return (
            f"完全覆盖={summary.get('fully_covered_count', 0)} "
            f"部分覆盖={summary.get('partially_covered_count', 0)} "
            f"缺失={summary.get('missing_count', 0)}"
        )
    if name == "ingest":
        return f"入库={out.get('internal_doc_id') or '-'} 状态={out.get('remote_state') or '-'} 标签={out.get('label') or '-'}"
    return ""


class _StageSeq:
    """Runs a weighted stage sequence, reporting scaled progress and timings.

    Both chains share this: the external four-stage one and the internal
    split+ingest one only differ in which stages they run and their weights.
    """

    def __init__(self, ctx: StageContext, weights: dict[str, float]) -> None:
        self.ctx = ctx
        self.weights = weights
        self.regs = ctx.repos.regulations
        self.raw_progress = ctx.progress
        self.offset = 0.0
        self.timings: dict[str, float] = {}

    def bind(self, name: str) -> None:
        weight = self.weights[name]

        def mapper(local: float, step: str, _w: float = weight) -> None:
            self.raw_progress(self.offset + _w * min(max(local, 0.0), 1.0), step)

        self.ctx.progress = mapper

    async def run_stage(self, name: str, fn, **kwargs: Any) -> dict[str, Any]:
        self.bind(name)
        await self.regs.update(self.ctx.reg_id, {f"{name}_state": "running"})
        logger.info("▶ [%s] 开始", name)
        started = time.monotonic()
        try:
            out = await fn(self.ctx, **kwargs) or {}
        except Exception as exc:
            self.timings[name] = time.monotonic() - started
            await self.regs.update(self.ctx.reg_id, {f"{name}_state": "failed"})
            logger.error(
                "✘ [%s] 失败 (%.1fs) —— %s: %s",
                name, self.timings[name], type(exc).__name__, exc,
            )
            raise
        self.timings[name] = time.monotonic() - started
        await self.regs.update(self.ctx.reg_id, {f"{name}_state": "done", **out})
        self.offset += self.weights[name]
        self.raw_progress(self.offset, f"{name} done")
        logger.success(
            "✔ [%s] 完成 (%.1fs) —— %s", name, self.timings[name], _stage_metrics(name, out),
        )
        return out

    def summary(self) -> str:
        return ", ".join(f"{name}={secs:.1f}s" for name, secs in self.timings.items())


async def run_chain(ctx: StageContext) -> dict[str, Any]:
    """Dispatch on the document kind recorded by the push.

    内规/附件 (docType 2/3) go to the corpus and are split locally; everything
    else runs the external four-stage chain.
    """
    if is_internal(ctx.regulation):
        return await run_internal_chain(ctx)
    return await run_external_chain(ctx)


async def run_external_chain(ctx: StageContext) -> dict[str, Any]:
    """Applicable ⇒ split → internalization → analysis; otherwise downstream
    stages are marked ``skipped`` (per-stage endpoints can still force them)."""
    regs = ctx.repos.regulations
    chain_started = time.monotonic()
    logger.info(
        "chain start —— %s《%s》",
        ctx.reg_id, ctx.regulation.get("title") or "未命名",
    )
    seq = _StageSeq(ctx, WEIGHTS)

    # Manual backend pushes (optType=手动) are applicable by definition — skip
    # the stage-1 LLM call and stamp the override directly.
    if ctx.payload.get("skip_applicability"):
        logger.info("▶ [applicability] 跳过 —— 手动推送(optType=手动)默认适用")
        await regs.update(ctx.reg_id, {
            "applicability_applicable": True,
            "applicability_confidence": 1.0,
            "applicability_reason": "手动推送(optType=手动),默认适用于平安科技",
            "applicability_state": "done",
        })
        seq.offset = WEIGHTS["applicability"]
        seq.raw_progress(seq.offset, "applicability skipped (manual push)")
        applicable = True
    else:
        app = await seq.run_stage("applicability", applicability.run)
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
        seq.raw_progress(seq.offset, "生成文档摘要")
        await doc_profile.ensure(ctx)
        logger.warning(
            "chain stopped after stage 1 for %s: %s (%.1fs)",
            ctx.reg_id, reason, time.monotonic() - chain_started,
        )
        return {"chain_stopped": reason}

    if not ctx.rag.available:
        raise StageError("RAG 服务未配置 —— 请检查 RAG_PROVIDER 与连接配置（RAG_API_BASE / ZEUS_*）后重启服务")

    await seq.run_stage("split", split_stage.run)
    await seq.run_stage("internalization", internalize.run)
    await seq.run_stage("analysis", analysis.run)
    logger.success(
        "chain done —— %s in %.1fs (%s)",
        ctx.reg_id, time.monotonic() - chain_started, seq.summary(),
    )
    return {"chain_completed": True}


async def run_internal_chain(ctx: StageContext) -> dict[str, Any]:
    """内规/附件: split locally, then ingest into the corpus.

    Split runs first so the articles are persisted before the corpus upload: a
    failed ingest then fails the job while ``/result`` still returns them.
    """
    from clause_splitter.services import library_service

    ctx_reg = ctx.regulation
    kind = ctx_reg.get("push_doc_kind") or "internal"
    label = library_service.KIND_LABELS.get(kind, "")
    regs = ctx.repos.regulations
    chain_started = time.monotonic()
    logger.info(
        "内规/附件链条 start —— %s《%s》(kind=%s, 语料标签=%s)",
        ctx.reg_id, ctx_reg.get("title") or "未命名", kind, label or "-",
    )

    # 适用性 / 内化 / 分析都是外规专属；写 skipped 而不是留着 not_run，免得
    # 运维以为漏跑了。
    skipped: dict[str, Any] = {"applicability_state": "skipped"}
    for name in ("applicability", "internalization", "analysis"):
        skipped.update(DERIVED_FIELDS[name])
    for name in ("internalization", "analysis"):
        skipped[f"{name}_state"] = "skipped"
    await regs.update(ctx.reg_id, skipped)

    seq = _StageSeq(ctx, INTERNAL_WEIGHTS)
    await seq.run_stage("split", split_stage.run)

    src = Path(ctx_reg["source_path"])
    data = await asyncio.to_thread(src.read_bytes)
    filename = ctx_reg.get("source_filename") or src.name

    async def ingest(_ctx: StageContext) -> dict[str, Any]:
        doc, _created = await library_service.upload(
            data, filename, kind=kind, strict=True,
        )
        return {
            "internal_doc_id": doc["_id"],
            "remote_state": doc.get("remote_state") or "",
            "label": label,
        }

    try:
        await seq.run_stage("ingest", ingest)
    except RagError as exc:
        raise StageError(f"内规入库失败: {exc}") from exc

    logger.success(
        "内规/附件链条 done —— %s in %.1fs (%s)",
        ctx.reg_id, time.monotonic() - chain_started, seq.summary(),
    )
    return {"chain_completed": True, "ingested": True}
