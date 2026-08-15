"""异步任务状态模型."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TaskStatusEnum(str, Enum):
    """任务状态枚举 —— 与 Agent 流水线的各阶段对应."""

    PENDING = "pending"
    PARSING = "parsing"                    # 正在解析文档
    ANALYZING_STRUCTURE = "analyzing_structure"  # 正在分析文档结构
    PLANNING_BATCHES = "planning_batches"       # 正在规划批次
    DECOMPOSING = "decomposing"                 # 正在拆解条款
    VALIDATING = "validating"                   # 正在验证结果
    EXPORTING = "exporting"                     # 正在导出 Excel
    COMPLETED = "completed"
    FAILED = "failed"


class TaskInfo(BaseModel):
    """异步任务完整信息."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(description="唯一任务标识")
    task_type: str = Field(
        default="split",
        description="任务类型: split | compare | summary | pipeline",
    )
    status: TaskStatusEnum = Field(default=TaskStatusEnum.PENDING)
    document_name: str = Field(default="")
    progress: float = Field(
        default=0.0, ge=0.0, le=100.0,
        description="进度百分比",
    )
    current_step: str = Field(default="", description="当前步骤描述")
    total_articles: int = Field(default=0, ge=0)
    processed_articles: int = Field(default=0, ge=0)
    total_obligations: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)

    # Pipeline-specific: track which sub-phase is running
    pipeline_phase: str | None = Field(
        default=None,
        description="Pipeline phase: splitting | comparing | summarizing",
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    # 结果路径（暂不序列化到 API 响应中）
    result_json_path: str | None = Field(default=None, exclude=True)
    result_excel_path: str | None = Field(default=None, exclude=True)
    result_report_path: str | None = Field(default=None, exclude=True)

    def update_progress(
        self,
        status: TaskStatusEnum,
        progress: float,
        current_step: str = "",
        processed_articles: int | None = None,
        total_obligations: int | None = None,
    ) -> None:
        """更新任务进度."""
        self.status = status
        self.progress = min(progress, 100.0)
        self.current_step = current_step or status.value
        if processed_articles is not None:
            self.processed_articles = processed_articles
        if total_obligations is not None:
            self.total_obligations = total_obligations
        self.updated_at = datetime.now(timezone.utc)

    def mark_failed(self, error: str) -> None:
        """标记任务失败."""
        self.status = TaskStatusEnum.FAILED
        self.error_message = error
        self.updated_at = datetime.now(timezone.utc)

    def mark_completed(
        self,
        total_obligations: int = 0,
        json_path: str = "",
        excel_path: str = "",
        report_path: str = "",
    ) -> None:
        """标记任务完成."""
        self.status = TaskStatusEnum.COMPLETED
        self.progress = 100.0
        self.current_step = "处理完成"
        self.total_obligations = total_obligations
        self.result_json_path = json_path
        self.result_excel_path = excel_path
        self.result_report_path = report_path
        self.updated_at = datetime.now(timezone.utc)
