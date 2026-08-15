"""Searcher layer: build the set of retrieval backends to expose as tools.

``build_searchers`` returns a ``dict`` of the searchers that should be *bound*
as tools. The registry turns each entry into a ``f"{name}_search"`` tool.
``SearcherType.HYBRID`` is not a searcher class — it simply returns all three
backends so the LLM can autonomously choose which search tool to call each
turn. That choice lives in the bind-tools layer, not here.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable

from .base import BaseSearcher, Hit
from .bm25_search import BM25Searcher
from .embed import Embedder, embed_store
from .faiss_search import FaissSearcher
from .grep_search import GrepSearcher
from .store import Chunk, DocumentStore, Section, corpus_signature

__all__ = [
    "SearcherType",
    "build_searchers",
    "BaseSearcher",
    "Hit",
    "GrepSearcher",
    "BM25Searcher",
    "FaissSearcher",
    "Embedder",
    "embed_store",
    "Chunk",
    "Section",
    "DocumentStore",
    "corpus_signature",
]


class SearcherType(str, Enum):
    """Retrieval configuration. ``hybrid`` binds all three backends."""

    GREP = "grep"
    BM25 = "bm25"
    FAISS = "faiss"
    HYBRID = "hybrid"


def build_searchers(
    searcher_type: str | SearcherType,
    store: DocumentStore,
    rg_dir: str | None = None,
    embed_query: Callable[[str], object] | None = None,
    bm25_index_dir: str | None = None,
    faiss_index_dir: str | None = None,
) -> dict[str, BaseSearcher]:
    """Return the searchers to bind as tools, keyed by their tool-name prefix.

    Args:
        searcher_type: One of ``grep`` / ``bm25`` / ``faiss`` / ``hybrid``.
        store: The corpus store shared by all searchers.
        rg_dir: Materialized chunk directory (required for ``grep``).
        embed_query: Embedding callable (required for ``faiss``), e.g.
            ``Embedder().encode_query``.
        bm25_index_dir: Lucene index directory for the BM25 searcher (defaults
            to the store's per-corpus cache ``.cache/<cache_key()>/bm25``).
        faiss_index_dir: Persisted faiss index directory for the FAISS searcher
            (as written by ``embed_store``). When omitted, the searcher uses
            ``store.vectors``.
    """
    kind = SearcherType(searcher_type)
    if kind is SearcherType.GREP or kind is SearcherType.HYBRID:
        if rg_dir is None:
            raise ValueError("rg_dir is required to build the grep searcher.")
    if kind is SearcherType.FAISS or kind is SearcherType.HYBRID:
        if embed_query is None:
            raise ValueError("embed_query is required to build the faiss searcher.")

    if kind is SearcherType.GREP:
        return {"grep": GrepSearcher(store, rg_dir)}
    if kind is SearcherType.BM25:
        return {"bm25": BM25Searcher(store, index_dir=bm25_index_dir)}
    if kind is SearcherType.FAISS:
        return {"faiss": FaissSearcher(store, embed_query, index_dir=faiss_index_dir)}
    # hybrid: let the LLM pick the search tool per turn.
    return {
        "grep": GrepSearcher(store, rg_dir),
        "bm25": BM25Searcher(store, index_dir=bm25_index_dir),
        "faiss": FaissSearcher(store, embed_query, index_dir=faiss_index_dir),
    }
