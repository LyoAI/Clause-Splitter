"""Keyword searcher backed by ripgrep.

The store must be ``materialize``d to a directory of plain-text files (one per
chunk) plus a ``manifest.json`` mapping filenames to chunk ids. Search runs
ripgrep in literal ``-F`` mode — exact terms rather than patterns — and
ranks hits by their per-file match count.

Deviation from a naive ``--json`` + ``--max-count`` pipeline: count mode
(``-c --count-matches``) yields the *exact* per-file match count without the
``--max-count`` cap, so highly-relevant chunks don't tie with barely-relevant
ones. Only filenames and counts are streamed back; chunk text is read from the
store (single source of truth).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from time import perf_counter

from chillrag.utils import get_logger
from .base import BaseSearcher, Hit
from .store import DocumentStore

logger = get_logger()


class GrepSearcher(BaseSearcher):
    """Literal keyword search over materialized chunk files via ripgrep."""

    name = "grep"
    description = (
        "Literal keyword search over the corpus (exact match). Query with a single "
        "term as it appears in the documents; full sentences rarely match. "
        "Returns the top-k matching chunk snippets."
    )

    def __init__(self, store: DocumentStore, rg_dir: str | Path, manifest: dict | None = None) -> None:
        self.store = store
        self.rg_dir = Path(rg_dir)
        # Prefer an explicit manifest; fall back to one produced by materialize().
        self.manifest = manifest or store.manifest
        if not self.manifest:
            logger.error("no chunk manifest for corpus %r", store.name)
            raise ValueError(
                "GrepSearcher needs a manifest: call DocumentStore.materialize() first "
                "or pass one explicitly."
            )

    def search(self, query: str, k: int) -> list[Hit]:
        # -F: literal match (no regex), -g '*.txt': skip manifest.json.
        t0 = perf_counter()
        try:
            proc = subprocess.run(
                [
                    "rg", "-c", "--count-matches", "-F", "--no-heading",
                    "-g", "*.txt", query, str(self.rg_dir),
                ],
                capture_output=True,
                encoding="utf-8",
                timeout=30,
            )
        except FileNotFoundError as e:
            logger.error("ripgrep (rg) is not installed or not on PATH")
            raise RuntimeError("ripgrep (rg) is not installed or not on PATH.") from e

        # rc=1 means "no matches"; rc=2 means an error occurred.
        if proc.returncode == 2:
            logger.error("ripgrep failed: %s", proc.stderr.strip())
            raise RuntimeError(f"ripgrep failed: {proc.stderr.strip()}")

        hits: list[Hit] = []
        for line in proc.stdout.splitlines():
            # Count mode emits one "<path>:<count>" line per matching file.
            path, _, count = line.rpartition(":")
            chunk_id = self.manifest.get(Path(path).name)
            if chunk_id is None:
                # A file on disk with no manifest entry — stale materialization.
                logger.warning("grep match %s is not in the manifest", Path(path).name)
                continue
            chunk = self.store.get_chunk(chunk_id)
            if chunk is None:
                logger.warning("grep hit %r is not in the store", chunk_id)
                continue
            hits.append(Hit(score=float(count), chunk_id=chunk_id, text=chunk.text, meta=chunk.with_doc()))

        hits.sort(key=lambda h: h.score, reverse=True)
        hits = hits[:k]
        logger.info(
            "grep_search(query=%r, k=%d) -> %d hits in %.1fms",
            query, k, len(hits), (perf_counter() - t0) * 1000,
        )
        return hits
