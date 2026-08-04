"""Clause Splitter — 条款结构分析产品 主入口.

FastAPI 应用，提供条款拆分 API 服务。

启动方式：
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

端点：
    POST /api/v1/split              — 上传文件，返回 task_id
    GET  /api/v1/task/{task_id}     — 查询任务状态与进度
    GET  /api/v1/result/{task_id}/json  — 获取 JSON 结果
    GET  /api/v1/result/{task_id}/excel — 下载 Excel 文件
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env BEFORE any project imports — PySerini (imported via api.routes →
# pipeline.compare_pipeline → tools.search) needs JAVA_HOME + OPENAI_API_KEY
# set in os.environ at import time. Putting these in .env + calling load_dotenv
# early is the only setup needed; no manual export required.
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router as split_router
from utils import get_logger

logger = get_logger()


# ── 应用生命周期 ───────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理."""
    logger.info("Clause Splitter 服务启动中……")

    # Ensure output directory exists
    output_dir = Path(__file__).parent / "outputs"
    output_dir.mkdir(exist_ok=True)

    logger.info("服务就绪，等待请求……")
    yield

    # Shutdown
    logger.info("服务关闭")


# ── 应用工厂 ──────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用."""
    app = FastAPI(
        title="Clause Splitter — 条款结构分析与内外规比对",
        description="""
        将法律条文文件（Word/PDF）智能拆解为结构化的合规义务记录，
        并与内部规章制度进行语义匹配，生成差距分析报告。

        ## 功能

        - **条款结构分析**：自动识别章节、条款边界
        - **原子化义务提取**：一个动词 = 一条记录，主谓宾定状补拆分
        - **内外规比对**：外规义务 ↔ 内规条款语义匹配，覆盖度分析
        - **差距分析报告**：自动生成 Markdown 格式的专业分析报告
        - **Excel 导出**：严格按业务模板格式输出

        ## 使用方式

        1. `POST /api/v1/split` — 上传文件 → 获取 task_id
        2. `POST /api/v1/compare` — 上传内规 JSON + split task_id → 比对
        3. `POST /api/v1/summary` — 上传比对结果 + 原文 → 生成报告
        4. `POST /api/v1/pipeline` — 一键运行完整流水线
        5. `GET /api/v1/task/{task_id}` — 轮询进度
        6. `GET /api/v1/result/{task_id}/excel` — 下载 Excel
        7. `GET /api/v1/result/{task_id}/report` — 下载报告
        """,
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(split_router, prefix="/api/v1")
    logger.info("路由已注册")

    return app


# ── 应用实例 ───────────────────────────────────────────────────────────

app = create_app()


# ── 健康检查 ──────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """健康检查端点."""
    return {
        "status": "healthy",
        "service": "clause-splitter",
        "version": "0.1.0",
    }


# ── 命令行入口 ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import signal
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    logger.info("启动服务: %s:%d", host, port)
    server = uvicorn.Server(
        uvicorn.Config(
            "main:app",
            host=host,
            port=port,
            reload=os.environ.get("UVICORN_RELOAD", "false").lower() == "true",
            log_level="info",
            timeout_graceful_shutdown=3,
        )
    )

    # Ensure clean shutdown on Ctrl+C even when background threads are running
    def _shutdown(sig, frame):
        logger.info("收到退出信号，正在关闭服务……")
        server.should_exit = True
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    server.run()
