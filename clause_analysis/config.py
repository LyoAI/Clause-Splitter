"""Settings for the clause-analysis module (environment-overridable).

``RUN_ENV`` ("stg" / "prd" ...; empty = local dev) routes which env file
(``.{RUN_ENV}.env``) and model yaml (``<model>-<RUN_ENV>.yaml``) load.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from utils import get_logger

logger = get_logger()

REPO_ROOT = Path(__file__).resolve().parents[1]

RUN_ENV = os.getenv("RUN_ENV", "").strip().lower()


def _env_file(root: Path, run_env: str) -> Path:
    """.{run_env}.env when set (fallback .env), else .env."""
    if run_env:
        candidate = root / f".{run_env}.env"
        if candidate.is_file():
            return candidate
        logger.warning("RUN_ENV=%s but %s missing; falling back to .env", run_env, candidate.name)
    return root / ".env"


# Never overwrites variables already set in the environment.
load_dotenv(_env_file(REPO_ROOT, RUN_ENV))

# ── LLM ────────────────────────────────────────────────────────────────
LLM_CONFIG_DIR = REPO_ROOT / "config" / "llm"
# "openai" (OpenAI-compatible) or "pingan" (intranet gateway); names the yaml section.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "qwen3.6-27b")
# Per-request HTTP timeout (s).
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "180"))

# ── Episode budget ─────────────────────────────────────────────────────
# Wall-clock deadline for one clause (judgment + episode), seconds.
EPISODE_TIMEOUT_S = float(os.getenv("EPISODE_TIMEOUT_S", "600"))
# Tool-call budget per episode; hitting it forces a final answer.
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", "15"))
# Prompt language for tool hints: "zh" | "en".
PROMPT_LANG = os.getenv("PROMPT_LANG", "zh")

# ── Retrieval ──────────────────────────────────────────────────────────
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "10"))
# Snippet token cap for search results handed to the LLM.
SNIPPET_MAX_TOKENS = int(os.getenv("SNIPPET_MAX_TOKENS", "512"))

# ── Zeus knowledge base ────────────────────────────────────────────────
RAG_API_BASE = os.getenv("RAG_API_BASE", "")
RAG_TIMEOUT_S = float(os.getenv("RAG_TIMEOUT_S", "30"))
ZEUS_APP_ID = os.getenv("ZEUS_APP_ID", "")
ZEUS_APP_SECRET = os.getenv("ZEUS_APP_SECRET", "")
ZEUS_INDEX = os.getenv("ZEUS_INDEX", "")
# Raw recall candidates per search, and the vendor's max k.
ZEUS_KB_LIMIT = int(os.getenv("ZEUS_KB_LIMIT", "20"))
ZEUS_MAX_K = int(os.getenv("ZEUS_MAX_K", "50"))
# Score floors: recall filter and rerank (0 = omit).
ZEUS_RECALL_MIN_SCORE = float(os.getenv("ZEUS_RECALL_MIN_SCORE", "0.1"))
ZEUS_RERANK_MIN_SCORE = float(os.getenv("ZEUS_RERANK_MIN_SCORE", "0.0"))
# Documented gateway concurrency limit for this app.
ZEUS_MAX_CONCURRENCY = int(os.getenv("ZEUS_MAX_CONCURRENCY", "2"))
