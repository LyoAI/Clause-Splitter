"""条款比对服务 —— 外规 vs 内规语义匹配.

CompareService 接收已拆分的外规义务和内规义务，
使用 LLM 进行语义级别的条款匹配，输出覆盖度分析结果。
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
    """条款比对服务 —— 外规义务与内规义务的语义匹配."""

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
        external_document_name: str = "",
        internal_document_name: str = "",
        external_raw_text: str = "",
        internal_raw_text: str = "",
    ) -> CompareResult:
        """执行外规与内规的条款比对.

        Args:
            external_obligations: 外规拆分后的义务记录列表。
            internal_obligations: 内规（预拆分）的义务记录列表。
            external_document_name: 外规文档名称。
            internal_document_name: 内规文档名称。
            external_raw_text: 外规原文（可选，用于辅助 LLM 理解上下文）。
            internal_raw_text: 内规原文（可选，用于辅助 LLM 理解上下文）。

        Returns:
            CompareResult 包含每条外规义务的匹配状态。
        """
        if not self.llm:
            logger.error("CompareService: LLM 实例未注入")
            return CompareResult(
                external_document_name=external_document_name,
                internal_document_name=internal_document_name,
                total_external_obligations=len(external_obligations),
                matches=[],
            )

        system_prompt = get_system_prompt()
        all_matches: list[MatchResult] = []

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
            all_matches.extend(chunk_matches)
            logger.info(
                "Compare chunk %d/%d: %d matches parsed",
                i // _MAX_EXT_PER_CALL + 1,
                (len(external_obligations) + _MAX_EXT_PER_CALL - 1) // _MAX_EXT_PER_CALL,
                len(chunk_matches),
            )

        result = CompareResult(
            external_document_name=external_document_name,
            internal_document_name=internal_document_name,
            total_external_obligations=len(external_obligations),
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
