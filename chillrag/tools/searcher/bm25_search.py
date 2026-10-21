"""BM25 searcher backed by pyserini's Lucene index, with jieba pretokenization.

The Lucene index is derived from the DocumentStore at construction time and
cached on disk (keyed by a content signature) so unchanged corpora reuse it.
Scores come from Lucene BM25; text is always read back from the store, keeping
the store the single source of truth.

Chinese tokenization: chunks and queries are jieba-cut and space-joined, the
index is built with Lucene's ``-pretokenized`` mode (WhitespaceTokenizer), and
queries use a WhitespaceAnalyzer so terms align with the index — Lucene's
default analyzers split Chinese into single characters (or overlapping
bigrams) and neither does word segmentation, so any other analyzer here
silently returns zero hits. ``ANALYZER_VERSION`` is recorded in the index dir;
a tokenizer change (or an index built by an older release) forces a full
rebuild rather than silently serving a stale index.
"""

from __future__ import annotations

import os
from pathlib import Path
from time import perf_counter

# pyserini 0.24 eagerly constructs an OpenAI client at import time (for its
# optional encoders) and hard-fails unless an API key is set. We only need Lucene
# BM25, so guarantee a non-empty placeholder before pyserini loads; a real key
# already in the environment is left untouched.
os.environ.setdefault("OPENAI_API_KEY", "pyserini-bm25-placeholder")

from chillrag.config import doc_cache_dir
from chillrag.utils import get_logger
from .base import BaseSearcher, Hit
from .store import DocumentStore
from .tokenizer import ANALYZER_VERSION, to_pretokenized

logger = get_logger()


def _has_segments(index_dir: Path) -> bool:
    """A Lucene index exists iff it has committed ``segments_*`` files.

    Checking for *any* file is wrong: a crashed build can leave a stray
    ``write.lock`` (common on NAS filesystems where the lock itself failed),
    and treating that as a valid index makes us skip the build and then fail
    opening the dir with "no segments* file found".
    """
    return any(index_dir.glob("segments_*"))


def _recorded_analyzer(index_dir: Path) -> str:
    """The tokenizer version the existing index was built with ('' if none)."""
    marker = index_dir / "analyzer_version.txt"
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class BM25Searcher(BaseSearcher):
    """BM25 retrieval over a Lucene index built from the store's chunks."""

    name = "bm25"
    description = (
        "Lexical BM25 search over the corpus. Good for multi-word queries where "
        "keyword overlap matters and exact phrasing is unknown. Returns the top-k "
        "chunk snippets."
    )

    def __init__(
        self,
        store: DocumentStore,
        index_dir: str | Path | None = None,
        rebuild: bool = False,
    ) -> None:
        """Build (or load) a Lucene index derived from ``store``.

        Args:
            store: The corpus store; the index is derived from its chunks.
            index_dir: Where to build / find the Lucene index. Defaults to the
                store's cache directory: ``.cache/<store.cache_key()>/bm25``.
            rebuild: Force a rebuild even if ``index_dir`` already has an index.
        """
        self.store = store
        if index_dir is None:
            index_dir = doc_cache_dir(store.cache_key()) / "bm25"
        index_dir = Path(index_dir)
        index_dir.mkdir(parents=True, exist_ok=True)

        # Rebuild when the cached index is stale: no committed index, or it was
        # built with a different tokenizer version (mixing tokenizers inside one
        # Lucene index silently corrupts retrieval).
        analyzer_stale = _recorded_analyzer(index_dir) != ANALYZER_VERSION
        if rebuild or not _has_segments(index_dir) or analyzer_stale:
            t0 = perf_counter()
            logger.info(
                "Building BM25 Lucene index for %d chunks at %s",
                len(store.all_chunks()), index_dir,
            )
            self._build_index(index_dir)
            logger.info("BM25 index built in %.2fs", perf_counter() - t0)
        else:
            logger.info("Reusing cached BM25 Lucene index at %s", index_dir)

        self._index_dir = index_dir
        # Lazy import: pyserini boots a JVM (Lucene), so only pay it when a bm25
        # searcher is actually constructed — grep-only runs never import it.
        from pyserini.search.lucene import LuceneSearcher

        self._searcher = LuceneSearcher(str(index_dir))
        # The index is built with -pretokenized (jieba words space-joined), so
        # the query analyzer must split on whitespace only. Any other analyzer
        # re-tokenizes Chinese into single characters → 0 hits.
        from pyserini.pyclass import autoclass

        self._searcher.set_analyzer(
            autoclass("org.apache.lucene.analysis.core.WhitespaceAnalyzer")()
        )

    def _build_index(self, index_dir: Path) -> None:
        """Index every chunk as one Lucene document (id = chunk_id).

        Contents are jieba-pretokenized (space-joined words) and indexed with
        ``-pretokenized`` so each word survives as a single term.

        Build in a local temp dir first, then swap into ``index_dir``. Building
        directly on the target fails on network filesystems (NAS/SMB/NFS) that
        don't support OS file locks — Lucene's IndexWriter needs an exclusive
        ``write.lock`` there. The swap is a plain rename, so no lock is needed
        on the final path. The target directory is unchanged.
        """
        import shutil
        import tempfile
        from pyserini.index.lucene import LuceneIndexer

        # mkdtemp() honors TMPDIR; keep it on a local disk (default /tmp on
        # Linux) or this same "No locks available" failure moves to the temp dir.
        build_dir = Path(tempfile.mkdtemp(prefix="bm25_build_"))
        try:
            # With args passed, LuceneIndexer indexes from ``-index``; the
            # ``-pretokenized`` flag feeds each ``contents`` through the
            # WhitespaceTokenizer verbatim (the jieba cutting already happened).
            indexer = LuceneIndexer(
                str(build_dir), args=["-index", str(build_dir), "-pretokenized"]
            )
            indexer.add_batch_dict(
                [
                    {"id": c.chunk_id, "contents": to_pretokenized(c.text)}
                    for c in self.store.all_chunks()
                ]
            )
            indexer.close()
            # Never swap a broken build: a committed index always has segments_*.
            if not _has_segments(build_dir):
                logger.error("Lucene build produced no segments_* files in %s", build_dir)
                raise RuntimeError(
                    f"Lucene build produced no segments_* files in {build_dir}"
                )

            if index_dir.exists():
                shutil.rmtree(index_dir)
            index_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(build_dir), str(index_dir))
            (index_dir / "analyzer_version.txt").write_text(
                ANALYZER_VERSION, encoding="utf-8"
            )
        except Exception as e:
            logger.warning("BM25 build failed for %r; discarding %s (%s)",
                           self.store.name, build_dir, e)
            shutil.rmtree(build_dir, ignore_errors=True)
            raise

    def search(self, query: str, k: int) -> list[Hit]:
        t0 = perf_counter()
        # Word-level tokenization must match the index build: jieba-cut then
        # space-join, so the WhitespaceAnalyzer keeps each word as one term.
        analyzed_query = to_pretokenized(query)
        if not analyzed_query.strip():
            return []
        lucene_hits = self._searcher.search(analyzed_query, k)
        hits: list[Hit] = []
        for h in lucene_hits:
            chunk = self.store.get_chunk(h.docid)
            if chunk is None:
                # The Lucene index names a chunk the store does not hold.
                logger.warning("BM25 hit %r is not in the store", h.docid)
                continue
            hits.append(
                Hit(score=float(h.score), chunk_id=h.docid, text=chunk.text, meta=chunk.with_doc())
            )
        logger.info(
            "bm25_search(query=%r, k=%d) -> %d hits in %.1fms",
            query, k, len(hits), (perf_counter() - t0) * 1000,
        )
        return hits
