"""Base searcher abstract class — defines the search interface.

All searcher implementations (BM25, dense, hybrid) inherit from this base.
Makes it easy to swap search backends without changing agent code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseSearcher(ABC):
    """Abstract base class for all searchers.

    Defines the minimal interface that any search backend must implement.
    The Compare Agent depends on this interface, not concrete implementations.
    """

    @abstractmethod
    def search(self, query: str, k: int = 20) -> list[dict[str, Any]]:
        """Search for documents matching the query.

        Args:
            query: Free-text search query.
            k: Number of top results to return.

        Returns:
            List of dicts with keys: docid, score, obligation (the full dict).
        """
        ...

    @abstractmethod
    def get_by_id(self, docid: str) -> dict[str, Any] | None:
        """Retrieve a single document by its ID.

        Args:
            docid: Document ID in the index.

        Returns:
            Full obligation dict, or None if not found.
        """
        ...

    @property
    @abstractmethod
    def search_type(self) -> str:
        """Identifier for this searcher type (e.g. 'BM25', 'dense')."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Release resources (close index readers, etc.)."""
        ...
