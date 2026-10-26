"""RAG client error taxonomy.

Business code distinguishes three situations:
- not configured (no ``RAG_API_BASE``) — a deployment error with a clear message;
- unavailable (network / timeout / 5xx) — transient, parsing may fall back;
- response error (unexpected payload / 4xx) — contract mismatch, do not retry.
"""

from __future__ import annotations


class RagError(RuntimeError):
    """Base class for every RAG client failure."""


class RagNotConfigured(RagError):
    """``RAG_API_BASE`` is empty — retrieval cannot be used at all."""


class RagUnavailable(RagError):
    """The service could not be reached (connection error, timeout, 5xx)."""


class RagResponseError(RagError):
    """The service answered with an unexpected status or payload."""

    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class RagAuthError(RagResponseError):
    """401/403 from the service."""


class RagNotFound(RagResponseError):
    """404 from the service (unknown doc / endpoint)."""
