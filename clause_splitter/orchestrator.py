"""Core orchestrator — the single meeting point of engine wiring and pipeline.

Two responsibilities, one file:

1. **Engine wiring (seam)**: the only place (besides the docprep.ingest /
   compare.index preparation modules) that imports the vendored ``chillrag``.
   Searchers, tool registry and the ReAct agent are all built here; business
   code depends only on these products. ChillRAG stays generic — the compare
   system prompt / force-answer wording are passed in as injected arguments
   (``ReActAgent``'s ``system_prompt`` / ``force_answer_msg`` hooks), so the
   engine loop carries no domain assumptions.
2. **Chain orchestration** (formerly pipeline/chain.py): applicability →
   split → internalization → analysis in one job, with the manual-push skip
   for stage 1 and per-stage progress weighting.
"""

from __future__ import annotations

from typing import Any

# Import clause_splitter.config first: it loads .env into os.environ, and
# chillrag.config's load_dotenv never overrides existing vars — this order
# gives clause-side .env values priority.
from clause_splitter.config import CONTEXT_SCOPE_MAX, MAX_TOOL_CALLS, PROMPT_LANG, doc_cache_dir
from clause_splitter.pipeline import applicability, analysis, internalize, split_stage
from clause_splitter.pipeline.contexts import DERIVED_FIELDS, STAGES, StageContext
from clause_splitter.prompts import load, render
from clause_splitter.utils import get_logger

from chillrag.agent import ReActAgent
from chillrag.config import SEARCH_TOP_K, SNIPPET_MAX_TOKENS
from chillrag.tools import ToolRegistry
from chillrag.tools.searcher import build_searchers as _engine_searchers
from chillrag.tools.searcher.store import DocumentStore

logger = get_logger()


# ═══════════════════════════════════════════════════════════════════════
# Engine wiring
# ═══════════════════════════════════════════════════════════════════════


def build_searchers(store: DocumentStore, searcher: str = "hybrid") -> dict[str, Any]:
    """Build retrieval backends for ``searcher`` (hybrid = grep+bm25+faiss).

    Index locations match compare.index.py's build layout (missing faiss dir →
    None → the faiss searcher falls back to in-store vectors); a failed
    assembly downgrades to bm25 so comparison is never blocked.
    """
    cache_root = doc_cache_dir(store.cache_key(), root=store.index_root or None)
    kwargs: dict[str, Any] = {}
    if searcher in ("grep", "hybrid"):
        kwargs["rg_dir"] = str(cache_root / "chunks")
    if searcher in ("faiss", "hybrid"):
        kwargs["faiss_index_dir"] = str(cache_root / "faiss")
        try:
            from chillrag.config import EMBED_DEVICE, EMBED_MODEL
            from chillrag.tools.searcher.embed import Embedder

            kwargs["embed_query"] = Embedder(model_name=EMBED_MODEL, device=EMBED_DEVICE).encode_query
        except Exception as e:  # noqa: BLE001 — missing model degrades gracefully
            logger.warning("embedding unavailable, skipping faiss backend: %s", e)
    try:
        return _engine_searchers(searcher, store, bm25_index_dir=str(cache_root / "bm25"), **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning("searcher assembly failed (%s), falling back to bm25: %s", searcher, e)
        return _engine_searchers("bm25", store, bm25_index_dir=str(cache_root / "bm25"))


def build_registry(store: DocumentStore, searcher: str = "hybrid") -> ToolRegistry:
    """Searchers → tool registry (one ``<name>_search`` tool per backend + get_context)."""
    registry = ToolRegistry(
        build_searchers(store, searcher),
        store,
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
        lang=PROMPT_LANG,
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
        logger.info("chain stopped after stage 1 for %s: %s", ctx.reg_id, reason)
        return {"chain_stopped": reason}

    await run_stage("split", split_stage.run)
    await run_stage("internalization", internalize.run)
    await run_stage("analysis", analysis.run)
    return {"chain_completed": True}
