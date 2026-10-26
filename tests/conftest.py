"""Pytest configuration — load .env before any test imports.

Settings (LLM endpoints, RAG service URL) are read from the environment at
import time, so .env must be loaded before any test module imports them.
"""

from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root BEFORE any test imports
load_dotenv(Path(__file__).parent.parent / ".env")
