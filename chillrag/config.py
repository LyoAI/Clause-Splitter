"""Configuration for the local RAG service — env-driven, no secrets on disk.

Loads ``.env`` at the repo root and exposes the retrieval / parsing knobs. All
paths default relative to the repo so a fresh checkout works, and every one is
env-overridable so an intranet deployment can point at its own volumes without
editing code.

Every path knob resolves the same way (``_path``): ``~`` is expanded and a
relative value is anchored at the repo. That matters because this package is
also consumed from another project — an editable install imports ``chillrag``
straight out of this tree, and a cwd-relative path would then mean the
*consuming* project's directory.

Env names split into two families:

- **Retrieval-stack names** (``EMBED_MODEL``, ``MINERU_BACKEND``, ``CHUNK_*``):
  the names the underlying parsing and embedding stack already uses, kept as-is
  so an existing deployment's environment keeps working.
- **``CHILLRAG_``-prefixed**: knobs this service adds — storage roots and the
  answer path. See the README's Configure table.

No knob is domain-specific: this is a general document retriever, and retrieval
policy belongs to the caller, passed per call.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Package root: ``chillrag/config.py`` → the repo directory above it.
PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

# Load KEY=VALUE pairs from ``.env`` (never overwrites existing env vars).
load_dotenv(REPO_ROOT / ".env")


def resolve_path(raw: str, default: Path) -> Path:
    """Resolve one path value: ``~`` expanded, a relative value anchored here.

    Anchoring is what makes this package importable from another project. A
    relative value in ``.env`` (``./materials``) would otherwise be resolved by
    ``load_dotenv`` against whatever directory the *consuming* process was
    started in, so an importing project would silently read its own ``materials``
    and ``config/llm``. Every path knob resolves the same way, from anywhere.

    An empty value counts as unset, so a commented-out line in ``.env`` leaves
    the default in place.
    """
    raw = (raw or "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _path(variable: str, default: Path) -> Path:
    """One path knob from the environment (see ``resolve_path``)."""
    return resolve_path(os.getenv(variable, ""), default)


# ---- Storage roots -------------------------------------------------------
# Per-folder derived artifacts: <cache>/<folder>/{md, chunks, bm25, faiss}.
CACHE_DIR = _path("CHILLRAG_CACHE_DIR", REPO_ROOT / ".cache")
# Input documents, one directory per folder (the caller's "foldername").
MATERIALS_DIR = _path("CHILLRAG_MATERIALS_DIR", REPO_ROOT / "materials")
# Per-model LLM YAML: ``<dir>/<model>.yaml``. Values may reference env vars as
# ``${VAR}`` so credentials stay out of the files (see ``llm/llm_registry.py``).
LLM_CONFIG_DIR = _path("CHILLRAG_LLM_CONFIG_DIR", REPO_ROOT / "config" / "llm")

# ---- LLM -----------------------------------------------------------------
# Which section of ``config/llm/<model>.yaml`` answers via: "openai" (any
# OpenAI-compatible endpoint) or "pingan" (the intranet gateway). A file may
# hold both, so this is what picks between them — an intranet deployment with
# only a ``pingan:`` section has nothing under "openai" to fall back to.
LLM_PROVIDER = os.getenv("CHILLRAG_LLM_PROVIDER", "openai") or "openai"
# Per-call request timeout (seconds). Without it a stalled provider request
# rides the SDK's own long retry schedule.
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "180"))

# ---- Answer path ---------------------------------------------------------
# How ``answer_by_question`` produces its answer:
#   "agent"   — ReAct loop over the search + context tools (best quality)
#   "oneshot" — faiss top-k straight into one LLM call (much faster, for batch)
ANSWER_MODE = os.getenv("CHILLRAG_ANSWER_MODE", "agent")
# Tool-call budget per answer when ANSWER_MODE=agent; hitting it forces a final
# answer. Empty = unlimited (bounded only by ANSWER_TIMEOUT_S).
_max_tool_calls = os.getenv("CHILLRAG_MAX_TOOL_CALLS", "20")
MAX_TOOL_CALLS: int | None = int(_max_tool_calls) if _max_tool_calls.strip() else None
# Wall-clock deadline per answer (seconds), so a stalled episode cannot hang a
# caller's thread-pool worker forever.
ANSWER_TIMEOUT_S = float(os.getenv("CHILLRAG_ANSWER_TIMEOUT_S", "600"))

# ---- Retrieval -----------------------------------------------------------
# Which retrieval backends the *agent* may pick from when answering:
# "grep" | "bm25" | "faiss" | "hybrid". "hybrid" binds all three as separate
# tools so the model chooses per turn. (``search_by_question`` is a separate,
# dense-only path and ignores this.)
SEARCHER_TYPE = os.getenv("SEARCHER_TYPE", "hybrid")
# Fixed number of hits every search tool returns.
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "10"))
# Max tokens per search snippet exposed to the LLM (truncated in the registry).
SNIPPET_MAX_TOKENS = int(os.getenv("SNIPPET_MAX_TOKENS", "512"))

# ``search_by_question`` / ``normal_rag_recall`` result cap.
RECALL_TOP_K = int(os.getenv("CHILLRAG_RECALL_TOP_K", "10"))
# When a caller passes ``recall_content_types``, that filter is applied *after*
# scoring (the index has no native filter), so score a wider candidate pool than
# the caller asked for and keep the best matches that survive the filter.
RECALL_CANDIDATES = int(os.getenv("CHILLRAG_RECALL_CANDIDATES", "200"))

# ---- Embedding (dense index) --------------------------------------------
# Local models root (e.g. modelscope-downloaded models).
MODELS_DIR = _path("MODELS_DIR", Path.home() / "Models")
# Local sentence-transformer used to vectorize chunks; the chunker reuses its
# tokenizer to measure chunk budgets, so both stay consistent. A str, not a
# Path: sentence-transformers accepts either a local directory or a hub name.
EMBED_MODEL = str(_path("EMBED_MODEL", MODELS_DIR / "Qwen3-Embedding-0.6B"))
# Chunks per encode call.
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))
# Sentence-transformers device. "cpu" sidesteps the macOS MPS-backend OOM during
# long batch encodes; set "cuda" (or "auto") on servers with a GPU.
EMBED_DEVICE = os.getenv("EMBED_DEVICE", "cpu")

# ---- Parsing & chunking --------------------------------------------------
# MinerU local backend ("pipeline" | "vlm" | "hybrid"); the CLI picks the device.
# Offline/intranet runs also need MINERU_MODEL_SOURCE=local in the environment —
# the MinerU subprocess inherits it (see ``parser/mineru_parse.py``).
MINERU_BACKEND = os.getenv("MINERU_BACKEND", "pipeline")
# Directory name under ``doc_cache_dir(folder)`` holding MinerU's output.
MINERU_MD_DIRNAME = "md"
# Structure chunking: a ``title`` block with level <= CHUNK_LEVEL opens a new
# chunk; deeper sub-headings stay inside the current chunk.
CHUNK_LEVEL = int(os.getenv("CHUNK_LEVEL", "3"))
# Soft token budget per chunk (oversized sections are split, tables kept whole).
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
# Adjacent chunks smaller than this many tokens merge into the previous one when
# the section path matches. 0 = strict section boundaries, no merging.
CHUNK_MIN_TOKENS = int(os.getenv("CHUNK_MIN_TOKENS", "50"))

# ---- Logging -------------------------------------------------------------
LOG_LEVEL = os.getenv("CHILLRAG_LOG_LEVEL", os.getenv("DF_LOGGING_LEVEL", "INFO"))


def doc_cache_dir(folder: str) -> Path:
    """Root for one folder's derived artifacts: MinerU output, materialized
    chunks, the BM25 index and the dense index. Mirrors the per-folder layout of
    ``MATERIALS_DIR``."""
    return CACHE_DIR / folder
