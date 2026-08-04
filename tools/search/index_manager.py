"""Lucene index manager — build, open, and manage BM25 indexes.

Uses PySerini's LuceneIndexer to build a searchable index from
internal clause obligations. An in-memory obligation store maps
id → full obligation dict for O(1) detail retrieval.

The Lucene index stores (id, contents) for BM25 scoring only.
Full obligation data lives in the store (dict, ~20MB per 10K obls).
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from pyserini.index.lucene._indexer import LuceneIndexer

from config.settings import get_settings
from tools.search.bm25_searcher import BM25Searcher

logger = logging.getLogger(__name__)


class IndexManager:
    """Build and manage Lucene BM25 indexes for obligation retrieval.

    Lifecycle:
        1. build(obligations) → (index_path, obligation_store)
        2. open() → returns BM25Searcher with store
        3. cleanup() → delete index files

    Usage:
        mgr = IndexManager("/tmp/indexes")
        mgr.build(internal_obligations)
        searcher = mgr.open()
        results = searcher.search("数据安全", k=20)
        detail = searcher.get_by_id("42")
        searcher.close()
        mgr.cleanup()
    """

    def __init__(self, base_dir: str | None = None) -> None:
        """Initialize the index manager.

        Args:
            base_dir: Directory to store indexes. Defaults to
                      outputs/.indexes under project root.
        """
        settings = get_settings()
        self._base_dir = Path(base_dir) if base_dir else (
            Path(settings.project_root) / "outputs" / ".indexes"
        )
        self._index_path: str | None = None
        self._obligation_store: dict[str, dict[str, Any]] = {}
        self._num_docs: int = 0

    # ── Build ──────────────────────────────────────────────────────────

    def build(
        self, obligations: list[dict[str, Any]],
    ) -> str:
        """Build a Lucene BM25 index + in-memory store from obligations.

        Each obligation is indexed with a composite 'contents' field
        combining the most discriminative structured fields.

        Args:
            obligations: List of ClauseObligation dicts (13-field format).

        Returns:
            Path to the built index directory.

        Raises:
            ValueError: If obligations list is empty.
        """
        if not obligations:
            raise ValueError("Cannot build index from empty obligations list")

        # Generate unique index directory
        index_id = uuid.uuid4().hex[:8]
        self._index_path = str(self._base_dir / f"index_{index_id}")
        Path(self._index_path).mkdir(parents=True, exist_ok=True)

        logger.info(
            "Building BM25 index at %s from %d obligations...",
            self._index_path, len(obligations),
        )

        indexer = LuceneIndexer(self._index_path)
        self._obligation_store = {}
        indexed_count = 0

        for obl in obligations:
            doc = self._obligation_to_index_doc(obl)
            if doc is None:
                continue
            try:
                indexer.add_doc_dict(doc)
                # Store full obligation by its index ID
                obl_id = str(obl.get("index", doc.get("id", "")))
                self._obligation_store[obl_id] = obl
                indexed_count += 1
            except Exception as e:
                logger.warning(
                    "Failed to index obligation %s: %s",
                    obl.get("index", "?"), e,
                )

        indexer.close()
        self._num_docs = indexed_count

        logger.info(
            "BM25 index built: %d/%d obligations indexed, store=%d entries",
            indexed_count, len(obligations), len(self._obligation_store),
        )
        return self._index_path

    def _obligation_to_index_doc(
        self, obl: dict[str, Any],
    ) -> dict[str, str] | None:
        """Convert a ClauseObligation dict to an indexable document.

        The 'contents' field combines the most discriminative fields
        for effective BM25 keyword matching.

        Args:
            obl: ClauseObligation dict.

        Returns:
            {'id': str, 'contents': str} dict, or None if no text.
        """
        parts: list[str] = []

        for field in ("subject", "action_verb", "action_target", "source_text"):
            val = str(obl.get(field, "")).strip()
            if val and val != "/":
                parts.append(val)

        if not parts:
            # Fallback: use any string value
            for v in obl.values():
                if isinstance(v, str) and v.strip() and v != "/":
                    parts.append(v.strip())

        contents = " ".join(parts)
        if not contents.strip():
            return None

        obl_id = str(obl.get("index", len(self._obligation_store) + 1))
        return {"id": obl_id, "contents": contents}

    # ── Open ───────────────────────────────────────────────────────────

    def open(self) -> BM25Searcher:
        """Open the built index for searching.

        Returns a BM25Searcher with the in-memory obligation store
        for instant detail retrieval.

        Returns:
            BM25Searcher instance ready for queries.

        Raises:
            RuntimeError: If no index has been built yet.
        """
        if not self._index_path:
            raise RuntimeError("No index built yet. Call build() first.")
        if not Path(self._index_path).exists():
            raise RuntimeError(
                f"Index directory does not exist: {self._index_path}"
            )

        return BM25Searcher(
            index_path=self._index_path,
            obligation_store=self._obligation_store,
        )

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def index_path(self) -> str | None:
        """Return the current index path, or None if not built."""
        return self._index_path

    @property
    def num_docs(self) -> int:
        """Return the number of documents in the current index."""
        return self._num_docs

    @property
    def store_size(self) -> int:
        """Return the number of entries in the obligation store."""
        return len(self._obligation_store)

    # ── Cleanup ────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Delete the current index directory and clear the store."""
        if self._index_path and Path(self._index_path).exists():
            try:
                shutil.rmtree(self._index_path)
                logger.info("Cleaned up index: %s", self._index_path)
            except Exception as e:
                logger.warning("Failed to cleanup index: %s", e)
        self._index_path = None
        self._obligation_store = {}
        self._num_docs = 0
