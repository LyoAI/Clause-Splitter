"""异步任务状态 —— FastAPI 薄壳的内存任务表.

薄壳只负责 上传 → 后台任务 → 轮询 → 下载,不做磁盘持久化;
进程重启即清空(真实计算在 CLI / run_*.py,薄壳只是 HTTP 代理)。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskState:
    """一个后台任务的状态。"""

    task_id: str
    task_type: str  # "split" | "compare"
    document_name: str = ""
    status: str = "pending"  # pending | processing | completed | failed
    progress: float = 0.0  # 0..100
    current_step: str = ""
    total_articles: int = 0
    total_obligations: int = 0
    error_message: str | None = None
    result_json_path: str = ""
    result_excel_path: str = ""
    result_report_path: str = ""
    result_review_path: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """给 API 响应的公开字段(不暴露服务器路径细节)。"""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "document_name": self.document_name,
            "status": self.status,
            "progress": self.progress,
            "current_step": self.current_step,
            "total_articles": self.total_articles,
            "total_obligations": self.total_obligations,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def update(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        self.updated_at = time.time()


class TaskManager:
    """线程安全的内存任务表(后台线程写,请求线程读)。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskState] = {}
        self._lock = __import__("threading").Lock()

    def create(self, task_type: str, document_name: str = "") -> TaskState:
        task = TaskState(
            task_id=uuid.uuid4().hex[:12],
            task_type=task_type,
            document_name=document_name,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> TaskState | None:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> list[TaskState]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)

    def delete(self, task_id: str) -> bool:
        with self._lock:
            return self._tasks.pop(task_id, None) is not None

    def clear(self) -> int:
        with self._lock:
            n = len(self._tasks)
            self._tasks.clear()
        return n
