"""条款拆分服务 —— 编排 Agent 流水线.

ClauseSplitService 是外部调用的主入口：
1. 接收文件路径和配置
2. 编排 Agent graph 执行
3. 返回 ClauseSplitResult
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from agent.state import create_initial_state
from agent.graph import build_split_graph
from models.clause import ClauseSplitResult, ClauseObligation
from utils import get_logger

logger = get_logger()


class ClauseSplitService:
    """条款拆分服务 —— 编排整个拆分流水线."""

    def __init__(self, llm: Any = None) -> None:
        """初始化服务.

        Args:
            llm: LLM 实例（ChatOpenAI / PingAnLLM 等）。如果为 None，使用默认配置。
        """
        self.llm = llm

    async def split(self, file_path: str, document_name: str) -> ClauseSplitResult:
        """执行完整的条款拆分流水线.

        Args:
            file_path: 上传文件的路径。
            document_name: 原始文件名。

        Returns:
            ClauseSplitResult 包含所有提取的义务记录。
        """
        start_time = time.time()

        logger.info("开始拆分: %s", document_name)

        # 构建并运行 graph
        graph = build_split_graph()

        initial_state = create_initial_state(
            file_path=file_path,
            document_name=document_name,
            llm=self.llm,
        )

        config = {"configurable": {"thread_id": f"split-{document_name}"}}
        try:
            final_state = await graph.ainvoke(initial_state, config=config)
        except Exception as e:
            logger.error("流水线执行失败: %s", e)
            return ClauseSplitResult(
                document_name=document_name,
                total_articles=0,
                total_obligations=0,
                obligations=[],
                processing_notes=[f"流水线执行失败: {e}"],
                processing_time_seconds=time.time() - start_time,
            )

        # 构建结果
        obligations_data = final_state.get("obligations", [])
        obligations: list[ClauseObligation] = []
        validation_errors: list[str] = []

        for obl_dict in obligations_data:
            try:
                obligations.append(ClauseObligation(**obl_dict))
            except Exception as e:
                validation_errors.append(str(e))

        processing_notes: list[str] = []
        if validation_errors:
            processing_notes.append(
                f"{len(validation_errors)} 条记录 Pydantic 验证失败"
            )
        error_msg = final_state.get("error_message")
        if error_msg:
            processing_notes.append(f"错误: {error_msg}")

        excel_path = final_state.get("excel_path", "")

        result = ClauseSplitResult(
            document_name=document_name,
            total_articles=final_state.get("total_articles", 0),
            total_obligations=len(obligations),
            obligations=obligations,
            processing_notes=processing_notes,
            processing_time_seconds=round(time.time() - start_time, 1),
            model_used=getattr(self.llm, "model_name", "") if self.llm else "",
            excel_path=final_state.get("excel_path", ""),
        )

        logger.info(
            "拆分完成: %d 条义务, 耗时 %.1fs",
            result.total_obligations,
            result.processing_time_seconds,
        )

        return result
