"""FastAPI dependency injection — global singletons (LLM, TaskManager)."""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path

# Ensure .env is loaded (idempotent)
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from services.task_manager import TaskManager

# 全局 TaskManager 单例
_task_manager: TaskManager | None = None

# Storage dir for tasks (same as routes.UPLOAD_DIR)
STORAGE_DIR = Path(tempfile.gettempdir()) / "clause_splitter_uploads"


def get_task_manager() -> TaskManager:
    """获取全局 TaskManager 实例（懒初始化，自动恢复已有任务）."""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager(storage_dir=STORAGE_DIR)
    return _task_manager


@lru_cache()
def get_llm():
    """获取 LLM 实例（懒初始化 + 缓存）.

    优先使用环境变量配置，否则尝试 PingAn / OpenAI 兼容接口。
    """
    import os

    # 优先使用 DashScope / OpenAI 兼容接口
    api_key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get(
        "OPENAI_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    model = os.environ.get("LLM_MODEL", "qwen3-max")

    if api_key:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=0.1,  # 低温度以获得更一致的结构化输出
        )

    # 尝试 PingAn 平台
    try:
        from llm.simple_llm import get_llm as _get_simple_llm
        llm = _get_simple_llm()
        if llm:
            return llm
    except Exception:
        pass

    return None
