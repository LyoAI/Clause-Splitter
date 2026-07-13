"""FastAPI 请求/响应模型."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from models.task import TaskStatusEnum


class SplitRequest(BaseModel):
    """提交拆分任务的请求体（可选额外参数）."""

    model_config = ConfigDict(extra="forbid")

    document_name: str = Field(
        default="",
        description="可选：覆盖上传文件名作为文档名称",
    )
    model_name: str | None = Field(
        default=None,
        description="可选：指定使用的 LLM 模型",
    )


class SplitResponse(BaseModel):
    """提交任务后的即时响应."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(description="异步任务 ID，用于后续查询")
    status: TaskStatusEnum = Field(default=TaskStatusEnum.PENDING)
    message: str = Field(default="任务已提交，正在处理中……")


class TaskStatusResponse(BaseModel):
    """任务状态查询响应."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_type: str = Field(default="split")
    status: TaskStatusEnum
    document_name: str = ""
    progress: float = Field(default=0.0, ge=0.0, le=100.0)
    current_step: str = ""
    total_articles: int = 0
    processed_articles: int = 0
    total_obligations: int = 0
    estimated_remaining_seconds: float | None = None
    error_message: str | None = None
    pipeline_phase: str | None = Field(default=None)


class ErrorResponse(BaseModel):
    """标准错误响应."""

    model_config = ConfigDict(extra="forbid")

    error_code: str = Field(description="错误码")
    message: str = Field(description="错误描述")
    detail: str | None = Field(default=None, description="详细错误信息")


# ── Compare / Summary / Pipeline ─────────────────────────────────────────

class CompareRequest(BaseModel):
    """提交比对任务的请求."""

    split_task_id: str = Field(
        ...,
        description="已完成拆分的 split 任务 ID",
    )
    internal_document_name: str = Field(
        default="",
        description="内部制度文档名称",
    )


class CompareResponse(BaseModel):
    """提交比对任务后的即时响应."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: TaskStatusEnum = Field(default=TaskStatusEnum.PENDING)
    message: str = Field(default="比对任务已提交……")


class SummaryRequest(BaseModel):
    """提交报告生成任务的请求."""

    compare_task_id: str = Field(
        ...,
        description="已完成比对的 compare 任务 ID",
    )
    external_raw_text: str = Field(
        default="",
        description="外规条款原文（Markdown格式，用于报告引用）",
    )
    internal_raw_text: str = Field(
        default="",
        description="内规条款原文（Markdown格式，用于报告引用）",
    )


class SummaryResponse(BaseModel):
    """提交报告生成任务后的即时响应."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: TaskStatusEnum = Field(default=TaskStatusEnum.PENDING)
    message: str = Field(default="报告生成任务已提交……")


class PipelineResponse(BaseModel):
    """提交完整流水线后的即时响应."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: TaskStatusEnum = Field(default=TaskStatusEnum.PENDING)
    message: str = Field(default="流水线任务已提交（拆分→比对→报告）……")


class TaskListResponse(BaseModel):
    """任务列表响应."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(default=0)
    tasks: list[TaskStatusResponse] = Field(default_factory=list)
