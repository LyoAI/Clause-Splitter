"""BM25 searcher using PySerini LuceneSearcher + in-memory obligation store.

The Lucene index stores (id, contents) for BM25 scoring.
An in-memory dict maps id → full obligation dict for instant detail retrieval.

This split design keeps the index lean while providing O(1) access
to full obligation details. For 10K obligations, the store uses ~20MB RAM.

Usage:
    searcher = BM25Searcher("/path/to/index", obligation_store)
    results = searcher.search("数据安全管理", k=20)
    detail = searcher.get_by_id("42")
    searcher.close()
"""

from __future__ import annotations

import logging
from typing import Any

from pyserini.search.lucene import LuceneSearcher

from tools.search.base import BaseSearcher

logger = logging.getLogger(__name__)


class BM25Searcher(BaseSearcher):
    """BM25 searcher backed by PySerini LuceneSearcher + in-memory store.

    The Lucene index provides BM25 relevance scoring. The obligation_store
    (dict[id → obligation]) provides instant full-detail retrieval.

    This avoids the need to store full JSON in the Lucene index, which
    PySerini's LuceneIndexer doesn't support for custom schemas.

    Attributes:
        index_path: Path to the Lucene index directory.
        searcher: Underlying PySerini LuceneSearcher instance.
        _store: Dict mapping obligation id → full obligation dict.
    """

    def __init__(
        self,
        index_path: str,
        obligation_store: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the BM25 searcher.

        Args:
            index_path: Path to a pre-built Lucene index directory.
            obligation_store: Dict mapping obligation index → full obligation dict.
                              If None, get_by_id won't work.

        Raises:
            ValueError: If the index path does not exist or is invalid.
        """
        if not index_path:
            raise ValueError("index_path is required for BM25Searcher")

        self.index_path = index_path
        self._store: dict[str, dict[str, Any]] = obligation_store or {}
        self.searcher: LuceneSearcher | None = None

        logger.info("Initializing BM25 searcher: %s", index_path)

        try:
            self.searcher = LuceneSearcher(index_path)
        except Exception as exc:
            raise ValueError(
                f"Index '{index_path}' is not a valid Lucene index path."
            ) from exc

        try:
            num_docs = self.searcher.num_docs
            store_docs = len(self._store)
            logger.info(
                "BM25 searcher ready: %d docs in index, %d in store",
                num_docs, store_docs,
            )
        except Exception:
            logger.warning("Could not verify index — may be empty")

    # ── Search interface ───────────────────────────────────────────────

    def search(self, query: str, k: int = 20) -> list[dict[str, Any]]:
        """BM25 search — return top-K matching obligations.

        Uses the Lucene index for BM25 scoring, then enriches results
        with full obligation data from the in-memory store.

        Args:
            query: Free-text search query.
            k: Number of results to return (default 20, max 100).

        Returns:
            List of result dicts: {docid, score, obligation}
        """
        if not self.searcher:
            raise RuntimeError("Searcher not initialized")

        if not query or not query.strip():
            return []

        k = min(max(k, 1), 100)

        try:
            hits = self.searcher.search(query, k)
        except Exception as e:
            logger.error("BM25 search failed: %s", e)
            return []

        results: list[dict[str, Any]] = []
        for hit in hits:
            docid = str(hit.docid)
            obligation = self._store.get(docid, {})
            results.append({
                "docid": docid,
                "score": float(hit.score),
                "obligation": obligation,
            })

        return results

    def get_by_id(self, docid: str) -> dict[str, Any] | None:
        """Retrieve a single obligation by its index ID from the store.

        O(1) lookup — no Lucene I/O needed.

        Args:
            docid: The obligation index number.

        Returns:
            Full obligation dict, or None if not found.
        """
        return self._store.get(str(docid))

    # ── Resource management ────────────────────────────────────────────

    def close(self) -> None:
        """Release the underlying Lucene searcher resources."""
        if self.searcher:
            try:
                self.searcher.close()
            except Exception as e:
                logger.warning("Error closing BM25 searcher: %s", e)
            self.searcher = None

    @property
    def search_type(self) -> str:
        return "BM25"

    @property
    def num_docs(self) -> int:
        """Return the number of documents in the index."""
        if self.searcher:
            try:
                return self.searcher.num_docs
            except Exception:
                pass
        return 0

    def __repr__(self) -> str:
        return (
            f"BM25Searcher(index='{self.index_path}', "
            f"docs={self.num_docs}, store={len(self._store)})"
        )
