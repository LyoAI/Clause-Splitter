"""Shared searcher interface: the ``Hit`` format and the ``BaseSearcher`` ABC.

A searcher maps a query to a ranked list of ``Hit`` objects. A ``Hit`` carries
the **full** chunk text; truncation to a ``snippet`` happens exactly once, in
the registry layer, so every search tool exposes the same interface regardless
of backend (grep / bm25 / faiss).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from chillrag.utils import get_logger

logger = get_logger()


@dataclass
class Hit:
    """One retrieval result.

    Attributes:
        score: Relevance score (match count, BM25 score, or cosine similarity).
        chunk_id: Global chunk id, resolvable through the DocumentStore.
        text: Full chunk text. Never truncated at this layer.
        meta: Provenance — ``{doc, page, pages, section, table_ids, ...}``, plus
            the content types the chunk stage tagged.
    """

    score: float
    chunk_id: str
    text: str
    meta: dict


class BaseSearcher(ABC):
    """Interface every retrieval backend implements."""

    #: Tool name prefix, used by the registry to expose ``f"{name}_search"``.
    name: str = "base"
    #: Backend-specific hint that differentiates this searcher from the others.
    description: str = ""

    @abstractmethod
    def search(self, query: str, k: int) -> list[Hit]:
        """Rank the top-``k`` chunks for ``query``, best first."""
        raise NotImplementedError

    def get_description(self, k: int = 10) -> str:
        """LLM-facing tool description.

        The first sentence is the generic search description shared by all
        backends; the trailing clause names this backend so the LLM can choose
        among several bound search tools (hybrid mode).
        """
        return (
            f"Perform a search on a knowledge source. Returns top-{k} hits with "
            f"chunk_id, score, and a snippet (may be truncated based on token "
            f"limits); each hit also carries provenance meta (doc, page, section, "
            f"content types). Backend {self.name}: {self.description}"
        )
