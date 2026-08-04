"""差距分析报告生成服务.

SummaryService 接收比对结果和内外规原文，
使用 LLM 生成专业的 Markdown 格式差距分析报告。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from models.compare import CompareResult
from utils import get_logger

logger = get_logger()


class SummaryService:
    """差距分析报告生成服务."""

    def __init__(self, llm: Any = None) -> None:
        """初始化报告服务.

        Args:
            llm: LLM 实例（ChatOpenAI / PingAnLLM 等）。
        """
        self.llm = llm

    async def generate(
        self,
        compare_result: CompareResult,
        external_raw_text: str = "",
        internal_raw_text: str = "",
        output_dir: str = "",
    ) -> dict[str, Any]:
        """生成差距分析报告.

        Args:
            compare_result: 比对结果。
            external_raw_text: 外规原文（Markdown格式）。
            internal_raw_text: 内规原文（Markdown格式）。
            output_dir: 报告输出目录（可选，用于保存 .md 文件）。

        Returns:
            {"report": str (Markdown), "report_path": str}
        """
        from prompts.summary import get_system_prompt, format_user_prompt

        if not self.llm:
            logger.error("SummaryService: LLM 实例未注入")
            return {"report": "", "report_path": ""}

        system_prompt = get_system_prompt()

        # Build compare results JSON for the prompt
        compare_json = json.dumps(
            compare_result.to_summary_dict(),
            ensure_ascii=False,
            indent=2,
        )

        user_prompt = format_user_prompt(
            compare_result_json=compare_json,
            external_text_md=external_raw_text,
            internal_text_md=internal_raw_text,
        )

        try:
            response = self.llm.invoke([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ])
            report = response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            logger.error("Summary LLM call failed: %s", e)
            return {"report": "", "report_path": ""}

        logger.info("Summary report generated, %d chars", len(report))

        # Save report to file if output_dir is provided
        report_path = ""
        if output_dir:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            report_file = out / "内外规合规差距分析报告.md"
            report_file.write_text(report, encoding="utf-8")
            report_path = str(report_file)
            logger.info("Report saved to: %s", report_path)

        return {"report": report, "report_path": report_path}
