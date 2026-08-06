"""Compare Pipeline — retrieve-then-judge many-to-many matching.

Orchestrates the comparison flow:
1. Open/build Lucene BM25 index over the internal obligations
2. For each external obligation in parallel: deterministic BM25 recall of
   top-K internal candidates → one focused LLM judgment → evidence grounding
3. Assemble MatchResults into CompareResult
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from config.settings import get_settings
from tools.search.index_manager import IndexManager
from operators.compare.retrieval import retrieve_candidates
from operators.compare.judge import judge_obligation
from operators.compare.validation import ground_match
from models.compare import CompareResult, MatchResult
from utils import get_logger

logger = get_logger()


def _obligation_fingerprint(obl: dict[str, Any]) -> str:
    """Identity + a bounded content hash — detects both a different library
    AND a re-split of the same doc (same doc::index, changed source text)."""
    oid = IndexManager.make_obligation_id(obl)
    src = str(obl.get("source_text", "") or "")[:200]
    return f"{oid}|{src}"


def _index_matches_library(
    index_dir: str,
    internal_obligations: list[dict[str, Any]],
) -> bool:
    """True if the persistent index's store is EXACTLY the caller's library.

    The persistent index is only an accelerator: it may be reused as the
    search space only when it holds precisely the obligations the caller
    wants to compare against. Any difference — a different/subset library, a
    re-split doc, or a stale sidecar — means the caller's set must be indexed.
    """
    disk_store = IndexManager.load_store_from_disk(index_dir)
    if disk_store is None:
        return False
    passed = {_obligation_fingerprint(o) for o in internal_obligations}
    stored = {_obligation_fingerprint(o) for o in disk_store.values()}
    return bool(passed) and passed == stored


class ComparePipeline:
    """Many-to-many comparison pipeline with retrieve-then-judge.

    Per external obligation (parallel, semaphore-bounded):
    - recall: deterministic BM25 top-K candidates (no LLM)
    - judge: one focused LLM call (candidate table → row number)
    - ground: validation maps the row to real evidence, downgrades ungroundable
      coverage claims to 缺失 (hallucination guard)

    Usage:
        pipeline = ComparePipeline(llm=llm)
        result = await pipeline.run(
            external_obligations=ext_obls,
            internal_obligations=int_obls,
            external_document_names=["外规A"],
            internal_document_names=["内规A", "内规B"],
        )
    """

    def __init__(self, llm: Any = None) -> None:
        """Initialize the compare pipeline.

        Args:
            llm: LLM instance (must support tool calling / function calling).
        """
        self.llm = llm

    async def run(
        self,
        external_obligations: list[dict[str, Any]],
        internal_obligations: list[dict[str, Any]],
        external_document_names: list[str] | None = None,
        internal_document_names: list[str] | None = None,
        external_raw_text: str = "",
        internal_raw_text: str = "",
        output_dir: str | None = None,
        index_dir: str = "",
        rebuild_index: bool = False,
    ) -> CompareResult:
        """Run the comparison pipeline.

        Args:
            external_obligations: All external ClauseObligation dicts.
            internal_obligations: All internal ClauseObligation dicts.
            external_document_names: Source document names for externals.
            internal_document_names: Source document names for internals.
            external_raw_text: (unused, kept for API compat).
            internal_raw_text: (unused, kept for API compat).
            output_dir: If provided, save compare_result.json here.
            index_dir: Path for persistent BM25 index. If index exists here,
                       reuse it (skip rebuild) unless rebuild_index=True.
                       If empty, build temp index (old behavior).
            rebuild_index: Force rebuild even if index exists at index_dir.

        Returns:
            CompareResult with all matches and statistics.
        """
        start_time = time.time()
        settings = get_settings()
        ext_list = list(external_document_names or [])
        int_list = list(internal_document_names or [])

        ext_count = len(external_obligations)
        int_count = len(internal_obligations)

        logger.info(
            "ComparePipeline: %d external vs %d internal obligations "
            "(%d ext docs vs %d int docs)",
            ext_count, int_count, len(ext_list), len(int_list),
        )

        if not self.llm:
            logger.error("ComparePipeline: LLM 实例未注入")
            return CompareResult(
                external_documents=ext_list,
                internal_documents=int_list,
                total_external_obligations=ext_count,
                total_internal_obligations=int_count,
                matches=[],
            )

        if not external_obligations or not internal_obligations:
            logger.warning("ComparePipeline: 外规或内规为空")
            return CompareResult(
                external_documents=ext_list,
                internal_documents=int_list,
                total_external_obligations=ext_count,
                total_internal_obligations=int_count,
                matches=[],
            )

        # ═══════════════════════════════════════════════════════════
        # Phase 1: Open or build BM25 index
        # ═══════════════════════════════════════════════════════════
        #
        # Strategy:
        #   - Persistent index at index_dir exists → open it (FAST, offline-built)
        #   - Persistent index doesn't exist → build from internal_obligations (first-time setup)
        #   - No index_dir → build temp index (one-shot, old behavior)
        #
        # The obligation_store is ALWAYS rebuilt from the passed internal_obligations
        # to ensure it matches what the caller expects.
        #
        index_mgr = IndexManager()
        persistent = bool(index_dir)
        need_build = True

        if persistent and not rebuild_index and IndexManager.index_exists(index_dir):
            # ── Persistent index is only an ACCELERATOR, not a hard override ──
            # The caller's internal_obligations ARE the comparison library. Reuse
            # the persistent index ONLY when the two are exactly the same set;
            # otherwise honor the caller's library by building a temp index.
            info = IndexManager.get_index_info(index_dir)
            disk_store = IndexManager.load_store_from_disk(index_dir)

            if _index_matches_library(index_dir, internal_obligations):
                # ── FAST PATH ── caller's library IS the indexed library ──
                need_build = False
                index_mgr._index_path = index_dir
                index_mgr._obligation_store = disk_store
                index_mgr._num_docs = len(disk_store)
                logger.info(
                    "ComparePipeline Phase 1: reusing persistent index at %s "
                    "(%d docs, %.1f MB) — caller library matches",
                    index_dir, info.get("num_docs", 0), info.get("size_mb", 0),
                )
                # Sanity: the persisted store should equal the Lucene doc count
                index_docs = info.get("num_docs", 0)
                if index_docs > 0 and abs(index_docs - len(disk_store)) / max(index_docs, 1) > 0.15:
                    logger.warning(
                        "Index has %d docs but store has %d obligations — "
                        "consider --build-index --force to realign.",
                        index_docs, len(disk_store),
                    )
            else:
                # ── Caller provided a different / partial library ──
                # Honor it: build a TEMP index from the passed set. Never clobber
                # the persistent index with a one-off comparison library.
                logger.warning(
                    "ComparePipeline Phase 1: caller's library differs from the "
                    "persistent index (%d obligations) — building a temp index "
                    "from the passed set for this comparison. If this is the "
                    "intended default library, rebuild the persistent index: "
                    "--build-index --internal-dir outputs/internal/ --force.",
                    len(internal_obligations),
                )
                index_mgr = IndexManager()
                persistent = False

        if need_build:
            if persistent:
                logger.info(
                    "ComparePipeline Phase 1: Building BM25 index at %s "
                    "(first-time setup — will be reused on subsequent runs)...",
                    index_dir,
                )
            else:
                logger.info("ComparePipeline Phase 1: Building temp BM25 index...")

            try:
                index_path = index_mgr.build(
                    internal_obligations,
                    index_path=index_dir if persistent else "",
                )
                logger.info(
                    "BM25 index built: %d docs at %s",
                    len(internal_obligations), index_path,
                )
            except Exception as e:
                logger.error("Failed to build BM25 index: %s", e)
                index_mgr.cleanup()
                return CompareResult(
                    external_documents=ext_list,
                    internal_documents=int_list,
                    total_external_obligations=ext_count,
                    total_internal_obligations=int_count,
                    matches=[],
                )

        try:
            searcher = index_mgr.open()
            logger.info(
                "BM25 searcher ready: %d docs",
                searcher.num_docs,
            )
        except Exception as e:
            logger.error("Failed to open BM25 index: %s", e)
            return CompareResult(
                external_documents=ext_list,
                internal_documents=int_list,
                total_external_obligations=ext_count,
                total_internal_obligations=int_count,
                matches=[],
            )

        # ═══════════════════════════════════════════════════════════
        # Phase 2: per-obligation retrieve → judge → ground (parallel)
        # ═══════════════════════════════════════════════════════════
        #
        # Each external obligation runs independently:
        #   1. RETRIEVE — deterministic BM25, top-K candidates (no LLM)
        #   2. JUDGE — one focused LLM call: the obligation + its candidates
        #   3. GROUND — validation maps the candidate row to real evidence
        # This replaces the old ReAct batch agent, which exceeded its iteration
        # budget on larger batches and fabricated matches during the forced
        # synthesis pass (root cause of the mass-0% coverage bug).
        semaphore = asyncio.Semaphore(settings.llm_max_workers)

        async def _process_one(index: int) -> dict[str, Any]:
            obl = external_obligations[index]
            async with semaphore:
                candidates = retrieve_candidates(
                    searcher, obl, top_k=settings.compare_candidate_k,
                )
                if not candidates:
                    logger.info(
                        "External #%s: no candidates retrieved → 缺失",
                        obl.get("index"),
                    )
                    return ground_match(
                        obl,
                        {
                            "status": "缺失",
                            "matched_candidate_index": None,
                            "analysis": "内规库中未检索到语义相关的候选条款。",
                            "remedy_suggestion": (
                                "待人工复核：确认是否需要新增或修订内规条款以承接该外部义务。"
                            ),
                        },
                        [],
                    )
                judge_result = await judge_obligation(self.llm, obl, candidates)
                return ground_match(obl, judge_result, candidates)

        try:
            match_dicts = await asyncio.gather(*[
                _process_one(i) for i in range(ext_count)
            ])
        except Exception as e:
            logger.error("ComparePipeline per-obligation processing failed: %s", e)
            match_dicts = []

        # ═══════════════════════════════════════════════════════════
        # Phase 3: assemble MatchResults (grounding done in Phase 2)
        # ═══════════════════════════════════════════════════════════
        all_matches: list[MatchResult] = []
        for m_dict in match_dicts:
            if not m_dict:
                continue
            try:
                all_matches.append(MatchResult(**m_dict))
            except Exception as e:
                logger.warning("Skipping invalid MatchResult: %s", e)

        # ═══════════════════════════════════════════════════════════
        # Cleanup + build result
        # ═══════════════════════════════════════════════════════════
        searcher.close()
        if not persistent:
            index_mgr.cleanup()  # only cleanup temp indexes
        else:
            logger.info("Persistent index kept at %s (ready for next comparison)", index_dir)

        result = CompareResult(
            external_documents=ext_list,
            internal_documents=int_list,
            total_external_obligations=ext_count,
            total_internal_obligations=int_count,
            matches=all_matches,
        )

        elapsed = time.time() - start_time
        logger.info(
            "ComparePipeline done: %d matches in %.1fs → "
            "%d fully / %d partial / %d not covered (%.0f%%)",
            len(all_matches), elapsed,
            result.fully_covered_count,
            result.partially_covered_count,
            result.not_covered_count,
            result.coverage_rate * 100,
        )

        # Save result if output dir specified
        if output_dir:
            out_path = Path(output_dir) / "compare" / "compare_result.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                result.model_dump_json(indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("Compare result saved to %s", out_path)

        return result
