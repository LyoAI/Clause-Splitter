"""Colored console logging — one stdlib logger for the whole service.

The location field is ``%(pathname)s:%(lineno)d`` (an absolute path), so the
VSCode terminal makes it a clickable link. Levels < ERROR go to stdout,
ERROR/CRITICAL to stderr. ``configure_root`` applies the same look to
third-party loggers (uvicorn, requests, motor, ...).
"""

from __future__ import annotations

import logging
import os
import sys

import colorlog

# ---------- 1) custom SUCCESS level (25) ----------
SUCCESS_LEVEL_NUM = 25
logging.addLevelName(SUCCESS_LEVEL_NUM, "SUCCESS")


def _success(self, message, *args, **kwargs):
    if self.isEnabledFor(SUCCESS_LEVEL_NUM):
        # stacklevel=2 points the record at the caller, not at this shim.
        kwargs.setdefault("stacklevel", 2)
        self._log(SUCCESS_LEVEL_NUM, message, args, **kwargs)


logging.Logger.success = _success

# ---------- 2) stdout / stderr split ----------
class MaxLevelFilter(logging.Filter):
    """Pass records below ``max_level`` (stdout)."""

    def __init__(self, max_level):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.max_level


class MinLevelFilter(logging.Filter):
    """Pass records at or above ``min_level`` (stderr)."""

    def __init__(self, min_level):
        super().__init__()
        self.min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.min_level


# ---------- 3) colored formatter ----------
LOG_COLORS = {
    "DEBUG": "blue",
    "INFO": "white",
    "SUCCESS": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "red,bg_white",
}

_MESSAGE_COLORS = {
    "DEBUG": "blue",
    "INFO": "white",
    "SUCCESS": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "red",
}


def _make_colored_formatter() -> colorlog.ColoredFormatter:
    secondary = {
        "levelname": LOG_COLORS,
        "message": _MESSAGE_COLORS,
        # Fixed cyan, so the clickable location reads as metadata.
        "pathname": {k: "cyan" for k in LOG_COLORS},
        "lineno": {k: "cyan" for k in LOG_COLORS},
        "asctime": {k: "green" for k in LOG_COLORS},
    }
    return colorlog.ColoredFormatter(
        fmt=(
            "%(asctime_log_color)s%(asctime)s.%(msecs)03d%(reset)s"
            " | %(levelname_log_color)s%(levelname)-8s%(reset)s"
            " | %(pathname_log_color)s%(pathname)s:%(lineno_log_color)s%(lineno)d%(reset)s"
            " - %(message_log_color)s%(message)s%(reset)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors=LOG_COLORS,
        secondary_log_colors=secondary,
        style="%",
    )


def _resolve_level(level: str | None) -> str:
    return level or os.getenv("LOG_LEVEL") or os.getenv("DF_LOGGING_LEVEL") or "INFO"


# ---------- 4) the shared logger ----------
def get_logger(level: str | None = None) -> logging.Logger:
    """The ``clause_splitter`` logger: colored, absolute ``file:line``.

    Handlers are added once; every call re-syncs the level, so asking for DEBUG
    later still takes effect.
    """
    level = _resolve_level(level)
    logger = logging.getLogger("clause_splitter")
    logger.setLevel(level)
    logger.propagate = False  # root has its own handler (configure_root)

    if not logger.handlers:
        fmt = _make_colored_formatter()
        out = logging.StreamHandler(stream=sys.stdout)
        out.addFilter(MaxLevelFilter(logging.ERROR))
        out.setFormatter(fmt)
        err = logging.StreamHandler(stream=sys.stderr)
        err.addFilter(MinLevelFilter(logging.ERROR))
        err.setFormatter(fmt)
        logger.addHandler(out)
        logger.addHandler(err)

    for handler in logger.handlers:
        handler.setLevel(level)
    return logger


def configure_root(level: str | None = None) -> logging.Logger:
    """Give third-party loggers the same colored, clickable format."""
    level = _resolve_level(level)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_clause_splitter", False):
            handler.setLevel(level)
            _route_uvicorn()
            return root
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_make_colored_formatter())
    handler._clause_splitter = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    _route_uvicorn()
    return root


#: uvicorn installs its own handlers before importing the app; forwarding them
#: to the root handler keeps one format instead of two.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _route_uvicorn() -> None:
    for name in _UVICORN_LOGGERS:
        log = logging.getLogger(name)
        for handler in list(log.handlers):
            log.removeHandler(handler)
        log.propagate = True
