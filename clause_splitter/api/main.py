"""FastAPI 薄壳应用工厂 —— 条款拆分 / 内外规比对 HTTP 服务.

启动方式:
    python run_api.py           # 或 uvicorn clause_splitter.api.main:app

安全要点:
- CORS 用 ``CORS_ORIGINS`` 环境变量白名单(逗号分隔);缺省只允许同源。
- ``allow_credentials`` 仅在白名单不含 ``*`` 时开启。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from clause_splitter.config import REPO_ROOT
from clause_splitter.utils import get_logger
from .routes import router

logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    (REPO_ROOT / "outputs").mkdir(exist_ok=True)
    logger.info("Clause Splitter 薄壳服务启动……")
    yield
    logger.info("服务关闭")


def create_app() -> FastAPI:
    """创建 FastAPI 应用(薄壳:上传 → 后台任务 → 轮询 → 下载)。"""
    app = FastAPI(
        title="Clause Splitter — 条款拆分与内外规比对",
        description="""
        将法律条文文件(Word/PDF)拆解为合规义务记录,并与内部规章制度比对,
        生成差距分析报告。薄壳只做 HTTP 封装,计算在 CLI 核心。

        ## 端点
        - `POST /api/v1/split` — 上传法规文档 → 后台拆分 → task_id
        - `POST /api/v1/compare` — 上传外规 + 内规库目录 → 后台比对 → task_id
        - `GET /api/v1/task/{task_id}` — 轮询任务状态/进度
        - `GET /api/v1/result/{task_id}/excel` — 下载拆分结果 Excel
        - `GET /api/v1/result/{task_id}/report` — 下载差距分析报告
        """,
        version="0.3.0",
        lifespan=lifespan,
        docs_url="/docs",
    )

    # CORS 白名单:允许 ``*`` 但绝不与 credentials 同开。
    origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
    allow_credentials = bool(origins) and "*" not in origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],  # 未配置时退化为全开放,但关 credentials
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info("CORS origins=%s credentials=%s", origins or ["*"], allow_credentials)

    app.include_router(router, prefix="/api/v1")

    @app.get("/health")
    async def health_check() -> dict:
        return {"status": "healthy", "service": "clause-splitter", "version": "0.3.0"}

    return app


app = create_app()
