"""FastAPI application base: request tracing, unified response envelope, health check.

Ported from the PingAn http-server template. All successful responses are wrapped
as {code, status, data, msg}; errors carry a machine-readable error_code in data.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from common.loguru import configure_logger, logger, request_id
from config.config_app import CONFIG_APP
from config.config_env import RUN_ENV


class ApiError(Exception):
    """Business error rendered as the unified error envelope."""

    def __init__(self, status_code: int, message: str, error_code: str = "", details: dict | None = None):
        self.status_code = status_code
        self.message = message
        self.error_code = error_code or f"HTTP_{status_code}"
        self.details = details or {}
        super().__init__(message)


def ok(data=None, msg: str = "") -> dict:
    return {"code": "200", "status": "success", "data": data, "msg": msg}


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logger(CONFIG_APP.log_level)
    CONFIG_APP.data_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Service starting, env=%s data_dir=%s", RUN_ENV, CONFIG_APP.data_dir)

    from clause_splitter.db import ensure_indexes
    from clause_splitter.jobs import JobRunner
    from clause_splitter.pipeline.contexts import default_repos
    from clause_splitter.services import context as services
    from clause_splitter.services import integration_service, library_service

    repos = default_repos()
    runner = JobRunner(repos, workers=CONFIG_APP.job_workers)
    library_service.register_index_job(runner)
    integration_service.register(runner)
    services.init(repos, runner)
    try:
        await ensure_indexes()
    except Exception as exc:  # noqa: BLE001 — boot without the cluster; repos fail loudly later
        logger.warning("Mongo unavailable at startup (deferred): %s", exc)

    await runner.start()
    yield
    await runner.stop()
    services.reset()
    from common.mongo_async import close_mongo

    await close_mongo()
    logger.info("Service stopped")


app = FastAPI(
    title="Regulation Internalization Service",
    description="External-regulation applicability, clause splitting, internalization judgment and gap analysis against the internal rule library.",
    version="0.4.0",
    lifespan=lifespan,
)

from api.routes import integration, jobs, library, regulations  # noqa: E402

API_PREFIX = "/api/v1"
app.include_router(regulations.router, prefix=API_PREFIX)
app.include_router(library.router, prefix=API_PREFIX)
app.include_router(jobs.router, prefix=API_PREFIX)
app.include_router(integration.router, prefix=API_PREFIX)

if CONFIG_APP.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CONFIG_APP.cors_origins,
        allow_credentials="*" not in CONFIG_APP.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed_ms = (time.time() - start) * 1000
    if request.url.path != "/healthz":
        logger.info("%s | %s %s - Status: %s | Time: %.2fms",
                    request.client.host if request.client else "-",
                    request.method, request.url.path, response.status_code, elapsed_ms)
    return response


@app.middleware("http")
async def request_tracing(request: Request, call_next):
    rid = str(uuid.uuid4())
    request_id.set(rid)
    request.state.request_id = rid
    body_preview = None
    with logger.contextualize(request_id=rid):
        if request.method == "POST" and request.url.path != "/healthz":
            content_type = request.headers.get("content-type", "")
            logger.info("Request | %s %s | client=%s | content-type=%s | content-length=%s",
                        request.method, request.url.path,
                        request.client.host if request.client else "-",
                        content_type, request.headers.get("content-length", "-"))
            # Capture JSON bodies only; multipart uploads may be hundreds of MB.
            if content_type.startswith("application/json"):
                try:
                    raw = await request.body()
                    body_preview = raw.decode("utf-8", errors="replace")[:500]
                    logger.info("Request body preview: %s", body_preview)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not read request body: %s", exc)
        response = await call_next(request)
        if 400 <= response.status_code < 500 and request.url.path != "/healthz":
            logger.warning("HTTP %s | %s %s", response.status_code, request.method, request.url.path)
        return response


class _InterceptHandler(logging.Handler):
    """Route stdlib logging (engine modules) into loguru so all logs share one pipeline."""

    def emit(self, record: logging.LogRecord) -> None:
        logger.opt(depth=6, exception=record.exc_info).log(record.levelname, record.getMessage())


logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)


@app.get("/healthz", description="Health check")
async def healthz():
    return PlainTextResponse("ok")


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    logger.warning("ApiError %s: %s", exc.error_code, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": str(exc.status_code), "status": "error", "msg": exc.message,
                 "data": {"error_code": exc.error_code, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    logger.error("Validation failed | %s %s | errors=%s", request.method, request.url.path, exc.errors())
    return JSONResponse(
        status_code=400,
        content={"code": "400", "status": "error", "msg": "Invalid request parameters",
                 "data": {"error_code": "VALIDATION_ERROR", "details": {"errors": exc.errors()[:10]}}},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"code": "500", "status": "error", "msg": f"{exc}", "data": {"error_code": "INTERNAL_ERROR"}},
    )
