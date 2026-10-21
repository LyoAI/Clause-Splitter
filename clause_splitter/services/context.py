"""Process-wide service wiring — built once in the FastAPI lifespan.

Single-process deployment (uvicorn workers=1); modules import ``get_runner``
/ ``get_repos`` instead of constructing their own.
"""

from __future__ import annotations

from typing import Any

from clause_splitter.pipeline.contexts import Repos

_runner: Any = None
_repos: Repos | None = None


def init(repos: Repos, runner: Any) -> None:
    global _runner, _repos
    _runner = runner
    _repos = repos


def reset() -> None:
    global _runner, _repos
    _runner = None
    _repos = None


def get_runner() -> Any:
    if _runner is None:
        raise RuntimeError("services not initialized — call init() in the app lifespan")
    return _runner


def get_repos() -> Repos:
    if _repos is None:
        raise RuntimeError("services not initialized — call init() in the app lifespan")
    return _repos
