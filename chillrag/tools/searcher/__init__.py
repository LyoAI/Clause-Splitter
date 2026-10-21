"""Searcher layer: build the set of retrieval backends to expose as tools.

``build_searchers`` returns a ``dict`` of the searchers that should be *bound*
as tools. The registry turns each entry into a ``f"{name}_search"`` tool.
``SearcherType.HYBRID`` is not a searcher class — it simply returns all three
backends so the LLM can autonomously choose which search tool to call each
turn. That choice lives in the bind-tools layer, not here.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Callable

# faiss's loader probes the avx512/avx2 wheel variants first and logs a noisy
# ModuleNotFoundError for each one the wheel does not ship. Pinning the opt
# level skips the probe, so swigfaiss_avx512 / swigfaiss_avx2 are never even
# attempted and the portable build loads directly. An FAISS_OPT_LEVEL already
# in the environment wins (set "avx2" on a host whose wheel ships it).
os.environ.setdefault("FAISS_OPT_LEVEL", "generic")

from chillrag.utils import get_logger
from .base import BaseSearcher, Hit
from .bm25_search import BM25Searcher
from .embed import Embedder, append_to_index, embed_store
from .faiss_search import FaissSearcher
from .grep_search import GrepSearcher
from .store import Chunk, DocumentStore, Section, corpus_signature

__all__ = [
    "SearcherType",
    "searcher_kinds",
    "build_searchers",
    "BaseSearcher",
    "Hit",
    "GrepSearcher",
    "BM25Searcher",
    "FaissSearcher",
    "Embedder",
    "embed_store",
    "append_to_index",
    "Chunk",
    "Section",
    "DocumentStore",
    "corpus_signature",
]

logger = get_logger()


class SearcherType(str, Enum):
    """Retrieval configuration. ``hybrid`` binds all three backends."""

    GREP = "grep"
    BM25 = "bm25"
    FAISS = "faiss"
    HYBRID = "hybrid"


def searcher_kinds(searcher_type: str | SearcherType) -> list[str]:
    """Expand a configuration into the concrete backend names it binds.

    ``hybrid`` is not a backend — it is "bind all three and let the model
    choose per turn", so it expands to the other three names in a stable order.
    """
    kind = SearcherType(searcher_type)
    if kind is SearcherType.HYBRID:
        return [SearcherType.GREP.value, SearcherType.BM25.value, SearcherType.FAISS.value]
    return [kind.value]


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
            to the store's cache ``.cache/<cache_key()>/bm25``).
        faiss_index_dir: Persisted faiss index directory for the FAISS searcher
            (as written by ``embed_store``). When omitted, the searcher uses
            ``store.vectors``.
    """
    kind = SearcherType(searcher_type)
    if kind is SearcherType.GREP or kind is SearcherType.HYBRID:
        if rg_dir is None:
            logger.error("rg_dir is required to build the grep searcher")
            raise ValueError("rg_dir is required to build the grep searcher.")
    if kind is SearcherType.FAISS or kind is SearcherType.HYBRID:
        if embed_query is None:
            logger.error("embed_query is required to build the faiss searcher")
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
