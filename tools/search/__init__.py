"""Search tools — BM25 / Lucene retrieval.

Note: BM25Searcher and IndexManager depend on PySerini (which requires a JVM).
They are NOT imported eagerly here so that modules importing this package do not
need Java installed. Import them explicitly when actually used:
    from tools.search.index_manager import IndexManager
    from tools.search.bm25_searcher import BM25Searcher
"""
from tools.search.base import BaseSearcher

__all__ = ["BaseSearcher", "BM25Searcher", "IndexManager"]


def __getattr__(name: str):
    """Lazy module attribute access — defer heavy imports until actually used."""
    if name == "BM25Searcher":
        from tools.search.bm25_searcher import BM25Searcher
        return BM25Searcher
    if name == "IndexManager":
        from tools.search.index_manager import IndexManager
        return IndexManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
