"""Lucene index manager — build, open, and manage BM25 indexes.

Uses PySerini's LuceneIndexer to build a searchable index from
internal clause obligations. An in-memory obligation store maps
id → full obligation dict for O(1) detail retrieval.

The Lucene index stores (id, contents) for BM25 scoring only.
Full obligation data lives in the store (dict, ~20MB per 10K obls).

Lifecycle:
    ── Offline (after splitting internal regulations) ──
    mgr = IndexManager()
    mgr.build_from_directory("outputs/internal/", "outputs/internal/.bm25_index/")

    ── Online (when comparing against external regulations) ──
    mgr = IndexManager()
    mgr._index_path = "outputs/internal/.bm25_index/"
    mgr._load_obligation_store(internal_obligations)
    searcher = mgr.open()   # instant — index already exists
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
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
        index_path: str = "",
        append: bool = False,
    ) -> str:
        """Build a Lucene BM25 index + in-memory store from obligations.

        Each obligation is indexed with a composite 'contents' field
        combining the most discriminative structured fields.

        Args:
            obligations: List of ClauseObligation dicts (13-field format).
            index_path: If provided, build at this exact path (persistent).
                        Otherwise, generate a random path under base_dir.
            append: If True and index exists at index_path, append to it
                    instead of overwriting. New obligations are added to
                    the existing index without touching existing documents.

        Returns:
            Path to the built index directory.

        Raises:
            ValueError: If obligations list is empty.
        """
        if not obligations:
            raise ValueError("Cannot build index from empty obligations list")

        # Use explicit path (persistent) or generate random one
        if index_path:
            self._index_path = index_path
        else:
            index_id = uuid.uuid4().hex[:8]
            self._index_path = str(self._base_dir / f"index_{index_id}")

        # Determine mode
        exists = self.index_exists(self._index_path)
        actual_append = append and exists

        if actual_append:
            # Read manifest to get accurate previous count (store might be empty)
            old_manifest = self._read_manifest(self._index_path)
            prev_total = old_manifest.get("total_obligations", len(self._obligation_store))
            logger.info(
                "Appending %d obligations to existing index at %s "
                "(current total=%d)...",
                len(obligations), self._index_path, prev_total,
            )
            # Don't clear the store — extend it
            # Append must write in place (cannot swap) — requires a working lock
            build_target = self._index_path
            swap_after = False
        else:
            if append and not exists:
                logger.info(
                    "Append requested but index doesn't exist — creating new: %s",
                    self._index_path,
                )
            # Fresh build → clear the store
            self._obligation_store = {}
            old_manifest = {}
            prev_total = 0

            # If a specific persistent path was given, build in a LOCAL temp dir
            # first, then swap into place. This sidesteps Lucene's OS file locks
            # on network drives (SMB/NAS) and makes the swap atomic.
            if index_path:
                build_target = tempfile.mkdtemp(prefix="bm25_build_")
                swap_after = True
                logger.info(
                    "Building BM25 index for %s at local temp %s "
                    "(swap into place when done)...",
                    self._index_path, build_target,
                )
            else:
                # Random temp path under base_dir — build in place
                build_target = self._index_path
                swap_after = False
                logger.info(
                    "Building BM25 index at %s from %d obligations...",
                    self._index_path, len(obligations),
                )

        indexer = self._create_indexer(build_target, actual_append)
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

        # ── Swap temp build into final location ─────────────────────────
        if swap_after:
            self._swap_index(build_target, self._index_path)
            logger.info("Index ready at %s", self._index_path)

        self._num_docs = prev_total + indexed_count

        # ── Update manifest ────────────────────────────────────────────
        # Count which documents were indexed in this batch
        new_doc_counts: dict[str, int] = {}
        for obl in obligations:
            doc_name = obl.get("document_name", "")
            if doc_name:
                new_doc_counts[doc_name] = new_doc_counts.get(doc_name, 0) + 1

        if actual_append:
            # Merge with existing manifest
            merged = {**old_manifest.get("documents", {}), **new_doc_counts}
            self._write_manifest(self._index_path, merged, self._num_docs)
            logger.info(
                "Appended %d obligations (total=%d, store=%d entries)",
                indexed_count, self._num_docs, len(self._obligation_store),
            )
        else:
            self._write_manifest(self._index_path, new_doc_counts, self._num_docs)
            logger.info(
                "BM25 index built: %d/%d obligations indexed, store=%d entries",
                indexed_count, len(obligations), len(self._obligation_store),
            )
        return self._index_path

    def _swap_index(self, build_dir: str, target: str) -> None:
        """Move a fully-built index from a local temp dir into its final location.

        Args:
            build_dir: Directory with the freshly-built index (local disk).
            target: Final index path (may be on a network drive).

        Raises:
            RuntimeError: If the swap fails (e.g. target is locked by another
                          process on Windows).
        """
        target_path = Path(target)
        build_path = Path(build_dir)

        try:
            # Remove any stale index at the target first
            if target_path.exists():
                shutil.rmtree(target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(build_path), str(target_path))
        except OSError as e:
            # Clean up the temp dir on failure
            try:
                shutil.rmtree(build_dir)
            except OSError:
                pass
            raise RuntimeError(
                f"Index built successfully in temp ({build_dir}) but could not "
                f"move it to {target}. This usually means another process has "
                f"the target folder open (API server running, or another build "
                f"in progress). Stop it and retry.\nOriginal error: {e}"
            ) from e

    def append_obligations(
        self, obligations: list[dict[str, Any]],
    ) -> str:
        """Append obligations to the existing index.

        Convenience wrapper around build(..., append=True).
        Requires that an index has already been built or loaded.

        Args:
            obligations: New ClauseObligation dicts to add.

        Returns:
            Path to the updated index.

        Raises:
            RuntimeError: If no index path is set (call build first).
        """
        if not self._index_path:
            raise RuntimeError(
                "No index path set. Call build() or load an existing index first."
            )
        if not self.index_exists(self._index_path):
            raise RuntimeError(
                f"Index does not exist at {self._index_path}. "
                "Call build() first to create it."
            )
        return self.build(obligations, index_path=self._index_path, append=True)

    @staticmethod
    def _create_indexer(index_path: str, append: bool) -> LuceneIndexer:
        """Create a LuceneIndexer with retry + stale-lock recovery.

        Lucene needs an exclusive write lock on the index directory. The
        ``No locks available`` IOException occurs when:
          1. A crashed/interrupted run left a stale ``write.lock`` file.
          2. Another process is actively using the index (API server running,
             another --build-index in parallel).
          3. The index sits on a network drive (SMB/NFS) where Lucene's native
             OS file locks are not supported.

        Strategy: retry briefly (transient contention), then remove a stale
        lock file as a last resort, then raise a clear actionable error.

        Args:
            index_path: Lucene index directory.
            append: Whether to open in append mode.

        Returns:
            An open LuceneIndexer.

        Raises:
            RuntimeError: With a human-readable diagnosis if the lock can't
                          be acquired.
        """
        max_retries = 3
        lock_file = Path(index_path) / "write.lock"

        for attempt in range(max_retries):
            try:
                return LuceneIndexer(index_path, append=append)
            except Exception as e:
                msg = str(e)
                is_lock_issue = ("No locks available" in msg
                                 or "locked by another process" in msg.lower()
                                 or "LockObtainFailed" in msg)
                if not is_lock_issue:
                    raise  # not a lock problem — surface the real error

                if attempt < max_retries - 1:
                    logger.warning(
                        "Lock contention on %s (attempt %d/%d), retrying...",
                        index_path, attempt + 1, max_retries,
                    )
                    time.sleep(1.0)
                    continue

                # Last resort: remove stale lock file left by a crashed run
                if lock_file.exists():
                    logger.warning(
                        "Removing stale lock file after retries: %s", lock_file,
                    )
                    try:
                        lock_file.unlink()
                    except OSError as unlink_err:
                        logger.warning("Could not remove lock file: %s", unlink_err)
                try:
                    return LuceneIndexer(index_path, append=append)
                except Exception as e2:
                    raise RuntimeError(
                        f"Cannot lock Lucene index directory: {index_path}\n"
                        "Possible causes:\n"
                        "  1. Another process is using this index (the API server "
                        "main.py is running, or another --build-index is active). "
                        "Stop it and retry.\n"
                        "  2. A crashed run left a lock. Delete the folder and rebuild.\n"
                        "  3. The path is on a network drive (SMB/NAS) that does not "
                        "support file locks. Move outputs/ to a local disk and retry."
                    ) from e2

        raise RuntimeError(f"Failed to create indexer for: {index_path}")

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

    # ── Existence check ──────────────────────────────────────────────────

    @staticmethod
    def index_exists(index_path: str) -> bool:
        """Check if a valid Lucene index exists at the given path."""
        if not index_path or not Path(index_path).exists():
            return False
        # A valid Lucene index has segment files
        return any(Path(index_path).glob("segments_*"))

    # ── Manifest (tracks which documents are in the index) ───────────────

    @staticmethod
    def _manifest_path(index_dir: str) -> Path:
        """Return the path to the index manifest file."""
        return Path(index_dir) / "index_manifest.json"

    @classmethod
    def _read_manifest(cls, index_dir: str) -> dict[str, Any]:
        """Read the index manifest, or return empty dict if not found."""
        mp = cls._manifest_path(index_dir)
        if mp.exists():
            try:
                return json.loads(mp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    @classmethod
    def _write_manifest(
        cls, index_dir: str,
        doc_counts: dict[str, int],
        total_obligations: int,
    ) -> None:
        """Write the index manifest tracking which documents are indexed."""
        mp = cls._manifest_path(index_dir)
        mp.write_text(json.dumps({
            "documents": doc_counts,
            "total_obligations": total_obligations,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get_indexed_documents(self) -> set[str]:
        """Return the set of document names already in the index.

        Uses the manifest file (fast, no need to load obligations).
        Falls back to scanning the obligation store if no manifest.
        """
        if self._index_path and self.index_exists(self._index_path):
            manifest = self._read_manifest(self._index_path)
            docs = manifest.get("documents", {})
            if docs:
                return set(docs.keys())
            # Fallback: use obligation store
            if self._obligation_store:
                return {
                    obl.get("document_name", "")
                    for obl in self._obligation_store.values()
                    if obl.get("document_name")
                }
        return set()

    # ── Build from directory (offline, after splitting) ─────────────────

    def build_from_directory(
        self,
        internal_dir: str,
        index_path: str = "",
        append: bool = False,
    ) -> str:
        """Scan a directory of *_split_result.json files and build a BM25 index.

        This is the **offline** path:
        1. Split internal regulations → outputs/internal/
        2. Call build_from_directory("outputs/internal/") → builds index once
        3. Subsequent comparisons just open the pre-built index

        Args:
            internal_dir: Directory containing **/*_split_result.json files.
            index_path: Where to store the Lucene index.
                        Defaults to <internal_dir>/.bm25_index/.
            append: If True and index exists, only add obligations from
                    documents NOT already in the index (dedup by document_name).
                    Documents that are already indexed are skipped.

        Returns:
            Path to the built index.

        Raises:
            ValueError: If no split_result.json files found or all empty.
        """
        scan_dir = Path(internal_dir)
        if not scan_dir.is_dir():
            raise ValueError(f"Directory not found: {internal_dir}")

        json_files = sorted(scan_dir.glob("**/*_split_result.json"))
        if not json_files:
            raise ValueError(
                f"No *_split_result.json files found in {internal_dir}"
            )

        # Determine index path
        resolved_index_path = index_path or str(scan_dir / ".bm25_index")

        # Smart append: skip documents already in the index (uses manifest)
        if append and self.index_exists(resolved_index_path):
            # Set _index_path so _get_indexed_documents() can read the manifest
            old_index_path = self._index_path
            self._index_path = resolved_index_path
            known_docs = self._get_indexed_documents()
            self._index_path = old_index_path  # restore
            if known_docs:
                logger.info(
                    "Append mode: %d document(s) already indexed → will skip",
                    len(known_docs),
                )
        else:
            known_docs: set[str] = set()

        logger.info(
            "Scanning %d split result files in %s ...",
            len(json_files), internal_dir,
        )

        all_obligations: list[dict[str, Any]] = []
        doc_counts: dict[str, int] = {}
        skipped_docs = 0
        parse_errors = 0

        for fpath in json_files:
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to read %s: %s", fpath.name, e)
                parse_errors += 1
                continue

            if isinstance(data, dict):
                obls = data.get("obligations", [])
                doc_name = data.get("document_name", fpath.stem)
            elif isinstance(data, list):
                obls = data
                doc_name = fpath.stem
            else:
                continue

            if not obls:
                continue

            # Skip if already indexed (smart append)
            if known_docs and doc_name in known_docs:
                skipped_docs += 1
                logger.info("  ⏭️  Skip (already indexed): %s (%d obligations)", doc_name[:60], len(obls))
                continue

            doc_counts[doc_name] = len(obls)
            all_obligations.extend(obls)

        if parse_errors:
            logger.warning("%d files failed to parse", parse_errors)
        if skipped_docs:
            logger.info("%d document(s) skipped (already in index)", skipped_docs)

        if not all_obligations:
            if skipped_docs > 0:
                logger.info("All %d document(s) already indexed — nothing to append.", skipped_docs)
                return resolved_index_path
            raise ValueError(
                f"No obligations found in {len(json_files)} split result files "
                f"under {internal_dir}"
            )

        # Print summary
        logger.info("Documents to index:")
        for doc, count in sorted(doc_counts.items()):
            logger.info("  + %s: %d obligations", doc[:60], count)

        # Build (or append)
        start = time.time()
        result_path = self.build(
            all_obligations,
            index_path=resolved_index_path,
            append=append,
        )
        elapsed = time.time() - start

        logger.info(
            "Index %s in %.1fs: %d obligations → %s",
            "updated" if append else "built", elapsed,
            len(all_obligations), result_path,
        )

        return result_path

    @staticmethod
    def get_index_info(index_path: str) -> dict[str, Any]:
        """Return metadata about an existing index.

        Args:
            index_path: Path to the Lucene index directory.

        Returns:
            Dict with keys: exists, path, num_docs, size_mb.
        """
        path = Path(index_path)
        if not IndexManager.index_exists(index_path):
            return {
                "exists": False,
                "path": str(path),
                "num_docs": 0,
                "size_mb": 0,
            }

        # Count docs by opening a READ-ONLY searcher (never a writer!)
        num_docs = 0
        try:
            from pyserini.search.lucene import LuceneSearcher
            s = LuceneSearcher(str(path))
            num_docs = s.num_docs
            s.close()
        except Exception:
            pass

        # Calculate size
        total_size = 0
        for f in path.rglob("*"):
            if f.is_file():
                total_size += f.stat().st_size

        return {
            "exists": True,
            "path": str(path),
            "num_docs": num_docs,
            "size_mb": round(total_size / (1024 * 1024), 2),
        }

    def load_obligation_store(
        self, obligations: list[dict[str, Any]],
    ) -> int:
        """Load/reload the in-memory obligation store from a list of obligations.

        Called when reusing an existing index — the index provides BM25 scoring
        but the store provides O(1) detail retrieval.

        Args:
            obligations: List of ClauseObligation dicts.

        Returns:
            Number of entries loaded into the store.
        """
        self._obligation_store = {}
        for obl in obligations:
            obl_id = str(obl.get("index", ""))
            if obl_id:
                self._obligation_store[obl_id] = obl
        self._num_docs = len(obligations)
        logger.info(
            "Obligation store loaded: %d entries",
            len(self._obligation_store),
        )
        return len(self._obligation_store)

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
