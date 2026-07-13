"""异步任务管理器 —— 管理拆分任务的生命周期.

任务数据自动持久化到磁盘（JSON），进程重启后恢复。
可替换为 Redis / DB 做分布式部署。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.task import TaskInfo, TaskStatusEnum
from utils import get_logger

logger = get_logger()


class TaskManager:
    """异步任务管理器（disk-persisted）.

    每个任务的状态保存在 {storage_dir}/{task_id}/task.json 中，
    进程重启后自动扫描恢复。

    用法：
        manager = TaskManager(storage_dir="/tmp/clause_splitter_uploads")
        task = manager.create_task(document_name="测试.docx")
        manager.update(task_id, status=TaskStatusEnum.PROCESSING)
        result = manager.get(task_id)
    """

    def __init__(self, storage_dir: str | Path = "") -> None:
        self._tasks: dict[str, TaskInfo] = {}
        self._storage_dir = Path(storage_dir) if storage_dir else Path("/tmp/clause_splitter_uploads")
        self._restore_from_disk()

    # ── Persistence ──────────────────────────────────────────────────────

    def _task_file(self, task_id: str) -> Path:
        return self._storage_dir / task_id / "task.json"

    def _save_task(self, task_id: str) -> None:
        """Persist a single task to disk."""
        task = self._tasks.get(task_id)
        if task is None:
            return
        tf = self._task_file(task_id)
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_text(task.model_dump_json(indent=2), encoding="utf-8")

    def _restore_from_disk(self) -> None:
        """Scan storage dir and reload persisted tasks."""
        if not self._storage_dir.exists():
            return
        count = 0
        for task_dir in self._storage_dir.iterdir():
            if not task_dir.is_dir():
                continue
            tf = task_dir / "task.json"
            if not tf.exists():
                continue
            try:
                data = json.loads(tf.read_text(encoding="utf-8"))
                task = TaskInfo(**data)
                self._tasks[task.task_id] = task
                count += 1
            except Exception as e:
                logger.warning("无法恢复任务 %s: %s", task_dir.name, e)
        if count:
            logger.info("从磁盘恢复了 %d 个任务", count)

    # ── CRUD ─────────────────────────────────────────────────────────────

    def create_task(self, document_name: str = "", task_type: str = "split") -> TaskInfo:
        """创建一个新任务并持久化."""
        task_id = uuid.uuid4().hex[:12]
        task = TaskInfo(
            task_id=task_id,
            task_type=task_type,
            document_name=document_name,
            status=TaskStatusEnum.PENDING,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self._tasks[task_id] = task
        self._save_task(task_id)
        logger.info("任务已创建: %s (%s) [%s]", task_id, document_name, task_type)
        return task

    def get(self, task_id: str) -> TaskInfo | None:
        """获取任务信息."""
        return self._tasks.get(task_id)

    def update(
        self,
        task_id: str,
        status: TaskStatusEnum | None = None,
        progress: float | None = None,
        current_step: str | None = None,
        processed_articles: int | None = None,
        total_articles: int | None = None,
        total_obligations: int | None = None,
        pipeline_phase: str | None = None,
        **extra: Any,
    ) -> TaskInfo | None:
        """更新任务状态并自动持久化."""
        task = self._tasks.get(task_id)
        if task is None:
            logger.warning("任务不存在: %s", task_id)
            return None

        if status is not None:
            task.status = status
        if progress is not None:
            task.progress = min(progress, 100.0)
        if current_step is not None:
            task.current_step = current_step
        if processed_articles is not None:
            task.processed_articles = processed_articles
        if total_articles is not None:
            task.total_articles = total_articles
        if total_obligations is not None:
            task.total_obligations = total_obligations
        if pipeline_phase is not None:
            task.pipeline_phase = pipeline_phase

        task.updated_at = datetime.now(timezone.utc)
        self._save_task(task_id)
        return task

    def mark_completed(
        self,
        task_id: str,
        total_obligations: int = 0,
        json_path: str = "",
        excel_path: str = "",
        report_path: str = "",
    ) -> TaskInfo | None:
        """标记任务完成并持久化."""
        task = self._tasks.get(task_id)
        if task is None:
            return None

        task.status = TaskStatusEnum.COMPLETED
        task.progress = 100.0
        task.current_step = "处理完成"
        task.total_obligations = total_obligations
        task.result_json_path = json_path
        task.result_excel_path = excel_path
        task.result_report_path = report_path
        task.updated_at = datetime.now(timezone.utc)
        self._save_task(task_id)
        logger.info("任务完成: %s (%s)", task_id, task.task_type)
        return task

    def mark_failed(self, task_id: str, error: str) -> TaskInfo | None:
        """标记任务失败并持久化."""
        task = self._tasks.get(task_id)
        if task is None:
            return None

        task.mark_failed(error)
        self._save_task(task_id)
        logger.error("任务失败: %s → %s", task_id, error)
        return task

    def delete(self, task_id: str) -> bool:
        """删除任务并从磁盘清理."""
        if task_id in self._tasks:
            del self._tasks[task_id]
            tf = self._task_file(task_id)
            if tf.exists():
                tf.unlink()
            return True
        return False

    def get_all(self) -> list[TaskInfo]:
        """获取所有任务（按创建时间倒序）."""
        tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def list_by_type(self, task_type: str) -> list[TaskInfo]:
        """按类型筛选任务."""
        return [t for t in self.get_all() if t.task_type == task_type]

    def clear_all(self) -> int:
        """清空所有任务（内存 + 磁盘）."""
        count = len(self._tasks)
        for task_id in list(self._tasks.keys()):
            self.delete(task_id)
        logger.info("已清空全部 %d 个任务", count)
        return count

    def cleanup_old(self, max_age_seconds: float = 3600) -> int:
        """清理过期任务."""
        now = datetime.now(timezone.utc)
        to_delete = [
            tid for tid, task in self._tasks.items()
            if (now - task.created_at).total_seconds() > max_age_seconds
        ]
        for tid in to_delete:
            self.delete(tid)
        if to_delete:
            logger.info("清理了 %d 个过期任务", len(to_delete))
        return len(to_delete)
