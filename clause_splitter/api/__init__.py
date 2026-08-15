"""FastAPI 薄壳 —— 上传 → 后台任务 → 轮询 → 下载."""

from .main import app, create_app

__all__ = ["app", "create_app"]
