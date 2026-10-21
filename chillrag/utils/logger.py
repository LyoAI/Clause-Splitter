"""Console logger — colored, level-split stdout/stderr, configured once.

The logger is namespaced (``chillrag``) rather than rooted, so it can be embedded
in a larger application without capturing or colliding with the host's own
logging setup. Level comes from ``CHILLRAG_LOG_LEVEL``; handlers are attached on
first use and never duplicated.
"""

import sys
import logging
import colorlog

from chillrag.config import LOG_LEVEL

# ---------- 1) Custom SUCCESS level ----------
SUCCESS_LEVEL_NUM = 25
logging.addLevelName(SUCCESS_LEVEL_NUM, "SUCCESS")

def success(self, message, *args, **kwargs):
    if self.isEnabledFor(SUCCESS_LEVEL_NUM):
        self._log(SUCCESS_LEVEL_NUM, message, args, **kwargs)

logging.Logger.success = success

# ---------- 2) stdout / stderr split filters ----------
class MaxLevelFilter(logging.Filter):
    """Pass records with level < max_level (stdout handler)."""
    def __init__(self, max_level):
        super().__init__()
        self.max_level = max_level
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.max_level

class MinLevelFilter(logging.Filter):
    """Pass records with level >= min_level (stderr handler)."""
    def __init__(self, min_level):
        super().__init__()
        self.min_level = min_level
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.min_level

# ---------- 3) loguru-style colored formatter ----------
def _make_colored_formatter():
    # loguru-like colors: DEBUG/INFO cool, WARNING yellow, ERROR/CRITICAL red,
    # SUCCESS green.
    log_colors = {
        "DEBUG":    "blue",
        "INFO":     "white",
        "SUCCESS":  "green",
        "WARNING":  "yellow",
        "ERROR":    "red",
        "CRITICAL": "red,bg_white",
    }
    # Secondary coloring: level name and message by level, name/func/line cyan.
    secondary = {
        "levelname": log_colors,
        "message":   {
            "DEBUG":    "blue",
            "INFO":     "white",
            "SUCCESS":  "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "red",
        },
        # Fixed cyan, mimicking loguru's <cyan> tags.
        "name":      {k: "cyan" for k in log_colors},
        "funcName":  {k: "cyan" for k in log_colors},
        "lineno":    {k: "cyan" for k in log_colors},
        "asctime":   {k: "green" for k in log_colors},
    }


    return colorlog.ColoredFormatter(
        fmt=(
            "%(asctime_log_color)s%(asctime)s.%(msecs)03d%(reset)s"
            " | %(levelname_log_color)s%(levelname)-8s%(reset)s"
            " | %(name_log_color)s%(name)s%(reset)s"
            ":%(funcName_log_color)s%(filename)s%(reset)s"
            ":%(funcName_log_color)s%(funcName)s%(reset)s"
            ":%(lineno_log_color)s%(lineno)d%(reset)s"
            " - %(message_log_color)s%(message)s%(reset)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors=log_colors,
        secondary_log_colors=secondary,
        style="%",
    )

# ---------- 4) get_logger (loguru-style add) ----------
def get_logger(level: str = None) -> logging.Logger:
    """Return the shared ``chillrag`` logger.

    Console output is split (<ERROR to stdout, >=ERROR to stderr), colored and
    formatted close to loguru's default; handlers are added only once.
    """
    if level is None:
        level = LOG_LEVEL

    logger = logging.getLogger("chillrag")
    logger.setLevel(level)
    logger.propagate = False  # don't propagate to root (would duplicate output)

    if logger.handlers:
        # already initialized
        return logger

    colored_fmt = _make_colored_formatter()

    # stdout: DEBUG..WARNING
    h_out = logging.StreamHandler(stream=sys.stdout)
    h_out.setLevel(level)
    h_out.addFilter(MaxLevelFilter(logging.ERROR))
    h_out.setFormatter(colored_fmt)

    # stderr: ERROR/CRITICAL
    h_err = logging.StreamHandler(stream=sys.stderr)
    h_err.setLevel(level)
    h_err.addFilter(MinLevelFilter(logging.ERROR))
    h_err.setFormatter(colored_fmt)

    logger.addHandler(h_out)
    logger.addHandler(h_err)
    return logger