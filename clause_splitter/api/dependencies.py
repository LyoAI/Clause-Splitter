"""FastAPI 依赖注入 —— 任务表 + LLM facade."""

from __future__ import annotations

from functools import lru_cache

from .tasks import TaskManager


@lru_cache(maxsize=1)
def get_task_manager() -> TaskManager:
    """进程级单例任务表。"""
    return TaskManager()


def get_llm(model: str = "qwen3-max", provider: str = "openai"):
    """从 ``clause_splitter.llm`` 构建 LLM facade(不持有任何密钥)。"""
    from clause_splitter.llm import get_llm as _build
    return _build(model, provider)
