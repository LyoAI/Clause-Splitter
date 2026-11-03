"""The retrieval interface business code depends on.

The only implementation is Zeus (``providers/zeus.py``); it keeps every wire
detail to itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from .types import Chunk, Hit


class ChunkLookup(Protocol):
    """Read access to chunks already seen during one analysis call."""

    def get_chunk(self, chunk_id: str) -> Chunk | None: ...


class RagService(ABC):
    """Hybrid retrieval over the internal regulation corpus."""

    name: str = ""

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether the provider is configured enough to try."""

    @abstractmethod
    def search(self, query: str, k: int, labels: list[str] | None = None) -> list[Hit]:
        """Top-k internal chunks for ``query``, best score first.

        ``labels`` is an optional per-call corpus filter (backend-specific
        meaning); ``None`` / empty = the whole index.
        """
