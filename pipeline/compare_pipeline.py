"""Compare Pipeline — BM25 index + ReAct agent parallel batch matching.

Orchestrates the many-to-many comparison flow:
1. Build Lucene BM25 index from all internal obligations (once)
2. Split external obligations into batches
3. Run ReAct compare agent per batch in parallel (asyncio.gather)
4. Merge results into CompareResult
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from config.settings import get_settings
from tools.search.index_manager import IndexManager
from operators.compare.compare_agent import run_compare_batch
from models.compare import CompareResult, MatchResult, CoverageStatus
from utils import get_logger

logger = get_logger()


class ComparePipeline:
    """Many-to-many comparison pipeline with BM25 retrieval.

    Replaces the old CompareService with a scalable architecture:
    - BM25 index for fast candidate retrieval (handles 10K+ obligations)
    - ReAct agent per batch (LLM searches, reviews, matches)
    - Parallel batch execution (asyncio.gather)

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
        # Phase 1: Build BM25 index
        # ═══════════════════════════════════════════════════════════
        logger.info("ComparePipeline Phase 1: Building BM25 index...")
        index_mgr = IndexManager()
        try:
            index_path = index_mgr.build(internal_obligations)
            searcher = index_mgr.open()
            logger.info(
                "BM25 index ready: %d docs at %s",
                searcher.num_docs, index_path,
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

        # ═══════════════════════════════════════════════════════════
        # Phase 2: Split into batches + parallel compare
        # ═══════════════════════════════════════════════════════════
        batch_size = settings.compare_batch_size
        batches = [
            external_obligations[i:i + batch_size]
            for i in range(0, ext_count, batch_size)
        ]
        total_batches = len(batches)

        logger.info(
            "ComparePipeline Phase 2: %d batches (size=%d), "
            "parallel workers=%d",
            total_batches, batch_size, settings.llm_max_workers,
        )

        async def _process_batch(idx: int) -> list[dict[str, Any]]:
            batch = batches[idx]
            logger.debug(
                "Batch %d/%d: %d external obligations",
                idx + 1, total_batches, len(batch),
            )
            return await run_compare_batch(
                external_batch=batch,
                searcher=searcher,
                llm=self.llm,
                batch_index=idx,
                total_batches=total_batches,
            )

        # Run batches in parallel with concurrency control
        semaphore = asyncio.Semaphore(settings.llm_max_workers)

        async def _bounded_process(idx: int) -> list[dict[str, Any]]:
            async with semaphore:
                return await _process_batch(idx)

        try:
            batch_results = await asyncio.gather(*[
                _bounded_process(i) for i in range(total_batches)
            ])
        except Exception as e:
            logger.error("ComparePipeline batch processing failed: %s", e)
            batch_results = []

        # ═══════════════════════════════════════════════════════════
        # Phase 3: Merge results
        # ═══════════════════════════════════════════════════════════
        all_matches: list[MatchResult] = []
        for batch_result in batch_results:
            if not batch_result:
                continue
            for m_dict in batch_result:
                try:
                    match = MatchResult(**m_dict)
                except Exception as e:
                    logger.warning("Skipping invalid MatchResult: %s", e)
                    continue
                # Backfill provenance
                if not match.external_document_name:
                    for obl in external_obligations:
                        if str(obl.get("index", "")) == match.external_clause_id:
                            match.external_document_name = obl.get(
                                "document_name", "",
                            )
                            match.external_article_id = obl.get(
                                "clause_number", "",
                            )
                            break
                if match.matched_internal_id and not match.matched_internal_document_name:  # noqa: E501
                    for obl in internal_obligations:
                        if str(obl.get("index", "")) == match.matched_internal_id:
                            match.matched_internal_document_name = obl.get(
                                "document_name", "",
                            )
                            break
                all_matches.append(match)

        # ═══════════════════════════════════════════════════════════
        # Cleanup + build result
        # ═══════════════════════════════════════════════════════════
        searcher.close()
        index_mgr.cleanup()

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
