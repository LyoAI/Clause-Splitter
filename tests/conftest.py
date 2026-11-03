"""Pytest configuration — load .env before any test imports.

Settings (LLM endpoints, RAG service URL) are read from the environment at
import time, so .env must be loaded before any test module imports them.
"""

import pytest

from dotenv import load_dotenv

from pathlib import Path

# Load .env from project root BEFORE any test imports
load_dotenv(Path(__file__).parent.parent / ".env")


@pytest.fixture(autouse=True)
def _local_run_env(monkeypatch):
    """Tests always run in local mode: ignore a RUN_ENV exported in the shell."""
    monkeypatch.delenv("RUN_ENV", raising=False)
