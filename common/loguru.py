"""Loguru logging with per-request trace ids (from the PingAn http-server template)."""

import sys
from contextvars import ContextVar

from loguru import logger

request_id: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    return request_id.get()


def configure_logger(level: str = "INFO") -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file.path}:{line} | {message}",
        enqueue=True,
    )
    logger.patch(lambda record: record["extra"].update(request_id=get_request_id()))
