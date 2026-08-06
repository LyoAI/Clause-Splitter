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

from tools.search.base import BaseSearcher
from tools.search.tokenizer import to_pretokenized

logger = logging.getLogger(__name__)


def _require_lucene_searcher():
    """Lazily import PySerini's LuceneSearcher (requires a JVM).

    Deferred so that merely importing BM25Searcher does not require Java.
    """
    try:
        from pyserini.search.lucene import LuceneSearcher
        return LuceneSearcher
    except Exception as e:  # noqa: BLE001 — jnius raises at import time
        raise RuntimeError(
            "BM25 检索需要 Java 运行时 (PySerini/jnius)。\n"
            "请在 .env 中设置 JAVA_HOME 指向 JDK 21，或安装 JDK 后重试。\n"
            f"原始错误: {e}"
        ) from e


def _require_whitespace_analyzer():
    """Lazily import Lucene's WhitespaceAnalyzer via jnius.

    The index is built with ``-pretokenized`` (jieba words space-joined), so
    queries must be split on whitespace only — any other analyzer would
    re-tokenize Chinese into single characters and return zero hits.
    """
    try:
        from pyserini.pyclass import autoclass
        return autoclass("org.apache.lucene.analysis.core.WhitespaceAnalyzer")
    except Exception as e:  # noqa: BLE001 — jnius raises at import time
        raise RuntimeError(
            "无法加载 Lucene WhitespaceAnalyzer（需要 Java 运行时 PySerini/jnius）。\n"
            "请在 .env 中设置 JAVA_HOME 指向 JDK 21，或安装 JDK 后重试。\n"
            f"原始错误: {e}"
        ) from e


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
        self.searcher = None

        logger.info("Initializing BM25 searcher: %s", index_path)

        try:
            LuceneSearcher = _require_lucene_searcher()
            self.searcher = LuceneSearcher(index_path)
            # The index is built with -pretokenized (jieba words space-joined),
            # so the query analyzer must split on whitespace only. Any other
            # analyzer re-tokenizes Chinese into single characters → 0 hits.
            WhitespaceAnalyzer = _require_whitespace_analyzer()
            self.searcher.set_analyzer(WhitespaceAnalyzer())
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

        # Word-level tokenization must match the index build: jieba-cut then
        # space-join, so the WhitespaceAnalyzer keeps each word as one term.
        analyzed_query = to_pretokenized(query)
        if not analyzed_query.strip():
            return []

        try:
            hits = self.searcher.search(analyzed_query, k)
        except Exception as e:
            logger.error("BM25 search failed: %s", e)
            return []

        # The Lucene index is the only place that knows *which* ids exist, but
        # the in-memory store is the ONLY carrier of the full obligation data
        # (Lucene -pretokenized stores just the id). A hit whose obligation is
        # missing from the store is an index/store mismatch (e.g. the persistent
        # index was built from outputs/internal/ but the caller passed a
        # different/subset set of internal obligations). Such a hit cannot be
        # read back in full → drop it, so the LLM never sees an obligation it
        # can't get 原文数据 for.
        results: list[dict[str, Any]] = []
        dropped = 0
        for hit in hits:
            docid = str(hit.docid)
            obligation = self._store.get(docid)
            if obligation is None:
                dropped += 1
                continue
            results.append({
                "docid": docid,
                "score": float(hit.score),
                "obligation": obligation,
            })

        if dropped:
            logger.warning(
                "BM25 search: dropped %d/%d hit(s) missing from obligation store "
                "(index built from a different obligation set than the store).",
                dropped, len(hits),
            )

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

    def get_clause_context(
        self,
        docid: str,
        window: int = 0,
    ) -> list[dict[str, Any]]:
        """Return the full clause (条) a matched obligation belongs to.

        A single obligation is one atomic sentence extracted from an article.
        Legal coverage judgments usually need the whole article context — an
        external requirement may be split across several internal obligations
        within the same 条. This returns every obligation from the same
        document that shares the same ``clause_number`` (e.g. all sentences of
        "第十四条"), sorted by index.

        Args:
            docid: Composite obligation id ("制度A::7") to locate the clause.
            window: If the target has no clause_number (or it can't be matched),
                    fall back to a ±window index neighborhood within the same
                    document. 0 means context = the obligation itself only.

        Returns:
            List of obligation dicts (full 原文) sorted by index. Empty if the
            id is not in the store.
        """
        from tools.search.index_manager import IndexManager

        target = self._store.get(str(docid))
        if target is None:
            return []

        doc_name, idx_str = IndexManager.parse_obligation_id(str(docid))
        clause = str(target.get("clause_number", "") or "").strip()
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            idx = int(target.get("index", 0) or 0)

        if clause and doc_name:
            # Same 条 within the same document — the natural legal context unit.
            peers = [
                obl for obl in self._store.values()
                if obl.get("document_name") == doc_name
                and str(obl.get("clause_number", "")).strip() == clause
            ]
            peers.sort(key=lambda o: int(o.get("index", 0) or 0))
            return peers

        # No usable clause_number → fall back to an index window.
        if window > 0 and doc_name:
            peers = [
                obl for obl in self._store.values()
                if obl.get("document_name") == doc_name
                and abs(int(obl.get("index", 0) or 0) - idx) <= window
            ]
            peers.sort(key=lambda o: int(o.get("index", 0) or 0))
            return peers

        return [target]

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
