"""RAG access: the retrieval interface and its Zeus implementation."""

from .base import ChunkLookup, RagService
from .errors import (
    RagAuthError,
    RagError,
    RagNotConfigured,
    RagNotFound,
    RagResponseError,
    RagUnavailable,
)
from .types import Chunk, Hit

__all__ = [
    "Chunk",
    "ChunkLookup",
    "Hit",
    "RagAuthError",
    "RagError",
    "RagNotConfigured",
    "RagNotFound",
    "RagResponseError",
    "RagService",
    "RagUnavailable",
]
