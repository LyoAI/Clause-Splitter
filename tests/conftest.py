"""Pytest configuration — load .env before any test imports.

PySerini (imported by tools.search) needs JAVA_HOME + OPENAI_API_KEY
set in os.environ at import time. This conftest ensures .env is loaded
before any test module triggers the PySerini import chain.
"""

from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root BEFORE any test imports
load_dotenv(Path(__file__).parent.parent / ".env")
