"""RAG access: a provider-agnostic interface plus its configured implementation.

Business code imports :class:`RagService`; which provider runs is chosen by
``RAG_PROVIDER``.
"""

from __future__ import annotations

from clause_splitter.config import (
    RAG_API_BASE,
    RAG_API_PASSWORD,
    RAG_API_USER,
    RAG_CORPUS,
    RAG_PARSE_CORPUS,
    RAG_PARSE_TIMEOUT_S,
    RAG_PROVIDER,
    RAG_TIMEOUT_S,
    ZEUS_APP_ID,
    ZEUS_APP_SECRET,
    ZEUS_CREATED_BY,
    ZEUS_INDEX,
    ZEUS_KB_LIMIT,
    ZEUS_MAX_CONCURRENCY,
    ZEUS_MAX_K,
    ZEUS_RECALL_MIN_SCORE,
    ZEUS_RERANK_MIN_SCORE,
    ZEUS_SEARCH_LABELS,
    ZEUS_UPLOAD_TIMEOUT_S,
)

from .base import ChunkLookup, RagService
from .errors import (
    RagAuthError,
    RagError,
    RagNotConfigured,
    RagNotFound,
    RagResponseError,
    RagUnavailable,
)
from .providers.chillrag import ChillRagProvider
from .providers.zeus import ZeusProvider
from .types import Chunk, CorpusStats, DocInfo, Hit, ParsedDoc

_PROVIDERS = {
    "chillrag": lambda: ChillRagProvider(
        RAG_API_BASE,
        RAG_API_USER,
        RAG_API_PASSWORD,
        timeout_s=RAG_TIMEOUT_S,
        ingest_timeout_s=RAG_PARSE_TIMEOUT_S,
        corpus=RAG_CORPUS,
        parse_corpus=RAG_PARSE_CORPUS,
    ),
    "zeus": lambda: ZeusProvider(
        RAG_API_BASE,
        ZEUS_APP_ID,
        ZEUS_APP_SECRET,
        ZEUS_INDEX,
        timeout_s=RAG_TIMEOUT_S,
        upload_timeout_s=ZEUS_UPLOAD_TIMEOUT_S,
        kb_limit=ZEUS_KB_LIMIT,
        max_k=ZEUS_MAX_K,
        recall_min_score=ZEUS_RECALL_MIN_SCORE,
        rerank_min_score=ZEUS_RERANK_MIN_SCORE,
        max_concurrency=ZEUS_MAX_CONCURRENCY,
        created_by=ZEUS_CREATED_BY,
        # Empty (unset / all) = text_labels=[], the whole knowledge base.
        search_labels=ZEUS_SEARCH_LABELS,
    ),
}

_service: RagService | None = None


def build_rag_service() -> RagService:
    """Instantiate the provider selected by ``RAG_PROVIDER``."""
    try:
        factory = _PROVIDERS[RAG_PROVIDER]
    except KeyError:
        raise ValueError(
            f"未知的 RAG_PROVIDER={RAG_PROVIDER!r}(可选: {', '.join(_PROVIDERS)})"
        ) from None
    return factory()


def get_rag_service() -> RagService:
    global _service
    if _service is None:
        _service = build_rag_service()
    return _service


__all__ = [
    "Chunk",
    "ChunkLookup",
    "CorpusStats",
    "DocInfo",
    "Hit",
    "ParsedDoc",
    "RagAuthError",
    "RagError",
    "RagNotConfigured",
    "RagNotFound",
    "RagResponseError",
    "RagService",
    "RagUnavailable",
    "build_rag_service",
    "get_rag_service",
]
