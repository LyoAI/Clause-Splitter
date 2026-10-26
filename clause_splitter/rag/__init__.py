"""External RAG service — the only retrieval dependency of this service.

There is no local engine and no fake backend in production: parsing, chunking,
indexing and retrieval all run inside the remote service. The process-wide
client is created lazily from ``RAG_API_BASE``; tests inject their own
implementation through :func:`set_rag_service`.
"""

from __future__ import annotations

from clause_splitter.config import (
    RAG_API_BASE,
    RAG_API_KEY,
    RAG_PARSE_TIMEOUT_S,
    RAG_TIMEOUT_S,
)

from .client import HttpRagClient
from .errors import (
    RagAuthError,
    RagError,
    RagNotConfigured,
    RagNotFound,
    RagResponseError,
    RagUnavailable,
)
from .protocols import RagService
from .types import (
    Chunk,
    CorpusStats,
    DocInfo,
    Hit,
    ParsedDoc,
    RagCapabilities,
    Section,
)

_service: RagService | None = None


def build_rag_client() -> RagService:
    """Build the configured client (empty base URL → a not-configured client)."""
    return HttpRagClient(
        RAG_API_BASE,
        api_key=RAG_API_KEY,
        timeout_s=RAG_TIMEOUT_S,
        parse_timeout_s=RAG_PARSE_TIMEOUT_S,
    )


def get_rag_service() -> RagService:
    global _service
    if _service is None:
        _service = build_rag_client()
    return _service


def set_rag_service(service: RagService | None) -> None:
    """Replace the process-wide service (tests / integration wiring)."""
    global _service
    _service = service


__all__ = [
    "Chunk",
    "CorpusStats",
    "DocInfo",
    "Hit",
    "HttpRagClient",
    "ParsedDoc",
    "RagAuthError",
    "RagCapabilities",
    "RagError",
    "RagNotConfigured",
    "RagNotFound",
    "RagResponseError",
    "RagService",
    "RagUnavailable",
    "Section",
    "build_rag_client",
    "get_rag_service",
    "set_rag_service",
]
