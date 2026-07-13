"""条款比对服务 —— 外规 vs 内规语义匹配（支持多对多）.

CompareService 接收已拆分的外规义务和内规义务，
使用 LLM 进行语义级别的条款匹配，输出覆盖度分析结果。

Supports many-to-many: multiple external regulations can be compared
against multiple internal policies in a single call.
"""

from __future__ import annotations

import json
from typing import Any

from agent.prompts.compare import get_system_prompt, format_user_prompt
from models.clause import ClauseObligation
from models.compare import CompareResult, MatchResult, CoverageStatus
from utils import get_logger
from utils.file.json_utils import parse_json_from_text

logger = get_logger()

# Max obligations per LLM call to avoid context overflow
_MAX_EXT_PER_CALL = 20


class CompareService:
    """条款比对服务 —— 外规义务与内规义务的语义匹配.

    支持多对多比对：合并多个外规文档的义务记录与多个内规文档的义务记录，
    通过 LLM 一次性进行语义级别匹配。
    """

    def __init__(self, llm: Any = None) -> None:
        """初始化比对服务.

        Args:
            llm: LLM 实例（ChatOpenAI / PingAnLLM 等）。
        """
        self.llm = llm

    async def compare(
        self,
        external_obligations: list[dict[str, Any]],
        internal_obligations: list[dict[str, Any]],
        external_document_names: list[str] | None = None,
        internal_document_names: list[str] | None = None,
        external_raw_text: str = "",
        internal_raw_text: str = "",
    ) -> CompareResult:
        """执行外规与内规的条款比对（支持多对多）.

        每条 obligation dict 应包含 'document_name' 字段用于分组显示。
        比对结果中会回填文档来源信息。

        Args:
            external_obligations: 外规拆分后的义务记录列表（可来自多个文档）。
            internal_obligations: 内规（预拆分）的义务记录列表（可来自多个文档）。
            external_document_names: 外部文档名称列表（用于结果统计）。
            internal_document_names: 内部文档名称列表（用于结果统计）。
            external_raw_text: 外规原文（可选，用于辅助 LLM 理解上下文）。
            internal_raw_text: 内规原文（可选，用于辅助 LLM 理解上下文）。

        Returns:
            CompareResult 包含每条外规义务的匹配状态。
        """
        if not self.llm:
            logger.error("CompareService: LLM 实例未注入")
            return CompareResult(
                external_documents=external_document_names or [],
                internal_documents=internal_document_names or [],
                total_external_obligations=len(external_obligations),
                total_internal_obligations=len(internal_obligations),
                matches=[],
            )

        system_prompt = get_system_prompt()
        all_matches: list[MatchResult] = []

        # Build index maps for backfilling document provenance
        ext_index_map: dict[str, dict[str, Any]] = {}  # clause_id → obl dict
        int_index_map: dict[str, dict[str, Any]] = {}  # clause_id → obl dict
        for obl in external_obligations:
            cid = str(obl.get("index", ""))
            ext_index_map[cid] = obl
        for obl in internal_obligations:
            cid = str(obl.get("index", ""))
            int_index_map[cid] = obl

        # Process in chunks to avoid context overflow
        for i in range(0, len(external_obligations), _MAX_EXT_PER_CALL):
            chunk = external_obligations[i:i + _MAX_EXT_PER_CALL]
            user_prompt = format_user_prompt(
                external_obligations=chunk,
                internal_obligations=internal_obligations,
                external_raw_text=external_raw_text,
                internal_raw_text=internal_raw_text,
            )

            try:
                response = self.llm.invoke([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ])
                content = response.content if hasattr(response, "content") else str(response)
            except Exception as e:
                logger.error("Compare LLM call failed (chunk %d): %s", i // _MAX_EXT_PER_CALL, e)
                continue

            # Parse JSON array from LLM output
            chunk_matches = self._parse_comparison(content)
            # Backfill document provenance from input obligations
            chunk_matches = self._backfill_provenance(
                chunk_matches, ext_index_map, int_index_map,
            )
            all_matches.extend(chunk_matches)
            logger.info(
                "Compare chunk %d/%d: %d matches parsed",
                i // _MAX_EXT_PER_CALL + 1,
                (len(external_obligations) + _MAX_EXT_PER_CALL - 1) // _MAX_EXT_PER_CALL,
                len(chunk_matches),
            )

        result = CompareResult(
            external_documents=external_document_names or [],
            internal_documents=internal_document_names or [],
            total_external_obligations=len(external_obligations),
            total_internal_obligations=len(internal_obligations),
            matches=all_matches,
        )

        logger.info(
            "Compare done: %d obligations → %d fully / %d partial / %d not covered (%.0f%%)",
            result.total_external_obligations,
            result.fully_covered_count,
            result.partially_covered_count,
            result.not_covered_count,
            result.coverage_rate * 100,
        )
        return result

    @staticmethod
    def _backfill_provenance(
        matches: list[MatchResult],
        ext_index_map: dict[str, dict[str, Any]],
        int_index_map: dict[str, dict[str, Any]],
    ) -> list[MatchResult]:
        """Backfill document provenance fields from input obligation data.

        If the LLM didn't fill in external_document_name / external_article_id /
        matched_internal_document_name, we look them up from the input.
        """
        for m in matches:
            # Backfill external provenance
            if not m.external_document_name or not m.external_article_id:
                ext_obl = ext_index_map.get(m.external_clause_id)
                if ext_obl:
                    if not m.external_document_name:
                        m.external_document_name = ext_obl.get("document_name", "")
                    if not m.external_article_id:
                        m.external_article_id = ext_obl.get("clause_number", "")

            # Backfill internal provenance
            if m.matched_internal_id and not m.matched_internal_document_name:
                int_obl = int_index_map.get(m.matched_internal_id)
                if int_obl:
                    m.matched_internal_document_name = int_obl.get("document_name", "")

        return matches

    @staticmethod
    def _parse_comparison(content: str) -> list[MatchResult]:
        """Parse LLM output into a list of MatchResult objects."""
        import re

        # Try JSON parsing
        parsed = parse_json_from_text(content)
        if isinstance(parsed, list):
            results: list[MatchResult] = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                try:
                    results.append(MatchResult(**item))
                except Exception as e:
                    logger.warning("Skipping malformed MatchResult: %s", e)
            return results

        # Try extracting JSON block
        m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", content)
        if m:
            try:
                data = json.loads(m.group(1))
                if isinstance(data, list):
                    return [MatchResult(**item) for item in data if isinstance(item, dict)]
            except Exception:
                pass

        logger.warning("Could not parse comparison output as JSON array")
        return []
