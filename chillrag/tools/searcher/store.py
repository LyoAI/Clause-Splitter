"""Document data contract — the single source of truth for the corpus.

A corpus is a set of documents. Each document is split into *chunks* kept in
the original reading order; ``get_context`` relies on that order to return
windows around a chunk. Chunks carry provenance ``meta`` (page, section,
content types) and the chunk text itself.

Design notes:
- The DocumentStore is the *only* place that holds text. Searchers only hold
  their own retrieval structure (inverted index, FAISS index) and look text up
  here, instead of each searcher loading the whole corpus into memory.
- The store is pipeline-agnostic: it can be built in-memory (tests), loaded
  from JSONL (a shipped corpus), or materialized to per-chunk files (for the
  ripgrep searcher).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Iterable

import numpy as np

from chillrag.config import doc_cache_dir
from chillrag.utils import get_logger

logger = get_logger()


def corpus_signature(store: "DocumentStore", n: int = 12) -> str:
    """Short content hash of a store's chunks; cache key for on-disk derived
    artifacts (Lucene BM25 index, faiss index). Rebuilds whenever any chunk's id
    or text changes. Shared so every cache is keyed the same way."""
    h = hashlib.md5()
    for c in store.all_chunks():
        h.update(c.chunk_id.encode("utf-8"))
        h.update(c.text.encode("utf-8"))
    return h.hexdigest()[:n]


@dataclass
class Chunk:
    """One atomic retrieval unit of a document.

    Attributes:
        chunk_id: Globally unique id, ``"{doc_id}#{order}"`` (e.g. "report.pdf#12").
        doc_id: The document this chunk belongs to.
        order: 0-based position in the document's reading order. ``get_context``
            windows are computed by shifting this order.
        text: Full chunk text (markdown, may include tables). Never truncated.
        meta: Provenance metadata — ``{page, pages, section, table_ids[,
            header, footer][, native_types, content_types]}``. ``page`` is the
            1-based page the chunk starts on, ``pages`` the page span (e.g. "45-47"), ``section``
            the heading hierarchy path (e.g. "Chapter 2 > Methods"),
            falling back to the doc id for heading-less documents,
            ``table_ids`` the reading-order table numbers embedded in the chunk
            (e.g. ["T2", "T3"]). ``header`` / ``footer`` hold the page banners
            (repeated page furniture is metadata, never chunk text).
    """

    chunk_id: str
    doc_id: str
    order: int
    text: str
    meta: dict = field(default_factory=dict)

    def with_doc(self) -> dict:
        """Provenance dict including the document name."""
        return {**self.meta, "doc": self.doc_id}


@dataclass
class Section:
    """One heading of a document's table of contents."""

    section_id: str
    title: str
    level: int  # 1 = top-level heading
    page: int | None = None


class DocumentStore:
    """Holds all chunks in reading order plus optional dense vectors.

    The store is the single source of truth. Dense vectors are optional and
    expected to be filled by the ingestion pipeline after chunks are added.

    ``name`` is the optional cache identity — the folder a corpus was loaded
    from; it decides where derived artifacts (bm25/faiss indexes, materialized
    chunks) are cached via ``cache_key()``.
    """

    def __init__(self, name: str | None = None) -> None:
        self.name = name
        # doc_id -> chunks in reading order
        self.docs: dict[str, list[Chunk]] = {}
        # chunk_id -> chunk (fast lookup)
        self.chunks_by_id: dict[str, Chunk] = {}
        # doc_id -> TOC sections in document order
        self.sections: dict[str, list[Section]] = {}
        # dense vectors for faiss (one row per chunk in ``all_chunks()`` order)
        self.vectors: np.ndarray | None = None
        # chunk_id for each row of ``vectors`` (parallel array)
        self.vector_ids: list[str] = []
        # filename -> chunk_id map, populated by ``materialize``
        self.manifest: dict[str, str] = {}

    # ------------------------------------------------------------------ add --

    def add_chunk(self, chunk: Chunk) -> None:
        """Insert one chunk, appending it to its document's reading order."""
        self.docs.setdefault(chunk.doc_id, []).append(chunk)
        self.chunks_by_id[chunk.chunk_id] = chunk

    def add_chunks(self, chunks: Iterable[Chunk]) -> None:
        for c in chunks:
            self.add_chunk(c)

    def add_section(self, doc_id: str, section: Section) -> None:
        self.sections.setdefault(doc_id, []).append(section)

    # ---------------------------------------------------------------- read --

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        return self.chunks_by_id.get(chunk_id)

    def get_context(self, chunk_id: str, scope: list[int]) -> list[Chunk]:
        """Return a window of chunks around ``chunk_id`` in reading order.

        ``scope`` is an inclusive relative window ``[start_offset, end_offset]``
        over chunk ``order`` — ``[-1, 1]`` means "one before + this + one after".
        The window is clamped to the document's chunk range and never crosses
        into another document. Returns an empty list if ``chunk_id`` is unknown.

        Args:
            chunk_id: Anchor chunk.
            scope: Two integers ``[start_offset, end_offset]`` (may be reversed).
        """
        chunk = self.get_chunk(chunk_id)
        if chunk is None:
            return []
        start, end = min(scope), max(scope)
        doc = self.docs[chunk.doc_id]
        lo = max(0, chunk.order + start)
        hi = min(len(doc) - 1, chunk.order + end)
        return doc[lo : hi + 1]

    def get_toc(self, doc_id: str) -> list[Section]:
        return self.sections.get(doc_id, [])

    def all_chunks(self) -> list[Chunk]:
        """All chunks, doc by doc in insertion order, each in reading order."""
        return [c for chunks in self.docs.values() for c in chunks]

    def cache_key(self) -> str:
        """Directory name for this store's derived cache: the store's ``name``
        when set (the folder it was loaded from), else the single document's id,
        else a content signature (multi-document corpus)."""
        if self.name:
            return self.name
        if len(self.docs) == 1:
            return next(iter(self.docs))
        return corpus_signature(self)

    # ------------------------------------------------------------ materialize --

    def materialize(self, out_dir: str | Path | None = None) -> dict[str, str]:
        """Write one plain-text file per chunk plus a ``manifest.json``.

        Files are laid out flat under ``out_dir`` so ripgrep can scan them in a
        single pass. Each file opens with a ``# meta: …`` header line carrying
        the chunk's provenance (page / pages / section / table_ids), so keyword
        search can hit on section paths that never appear in the chunk text
        itself, which is what makes a hit locatable. List meta
        values (``table_ids``) are comma-joined in the header. The returned (and
        cached) manifest maps ``filename`` to ``chunk_id``. Idempotent: files
        are overwritten.

        Args:
            out_dir: Directory for the chunk files. Defaults to the store's
                cache directory: ``.cache/<cache_key()>/chunks``.
        """
        out_dir = (
            Path(out_dir)
            if out_dir is not None
            else doc_cache_dir(self.cache_key()) / "chunks"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        t0 = perf_counter()
        manifest: dict[str, str] = {}
        for doc_id, chunks in self.docs.items():
            tag = hashlib.md5(doc_id.encode("utf-8")).hexdigest()[:8]
            stem = "".join(c if (c.isalnum() or c in "._-") else "_" for c in doc_id)
            for chunk in chunks:
                fname = f"{stem}__{tag}__{chunk.order:06d}.txt"
                meta_header = "; ".join(
                    f"{k}={','.join(v) if isinstance(v, list) else v}"
                    for k, v in chunk.meta.items()
                    if v not in (None, "")
                )
                content = f"# meta: {meta_header}\n{chunk.text}" if meta_header else chunk.text
                (out_dir / fname).write_text(content, encoding="utf-8")
                manifest[fname] = chunk.chunk_id
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.manifest = manifest
        logger.info(
            "Materialized %d chunk files to %s in %.1fms",
            len(manifest), out_dir, (perf_counter() - t0) * 1000,
        )
        return manifest

    # ---------------------------------------------------------------- I/O --

    def to_jsonl(self, out_dir: str | Path) -> None:
        """Serialize chunks and sections to ``chunks.jsonl`` / ``sections.jsonl``."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
            for c in self.all_chunks():
                f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
        with (out_dir / "sections.jsonl").open("w", encoding="utf-8") as f:
            for doc_id, sections in self.sections.items():
                for s in sections:
                    f.write(
                        json.dumps({"doc_id": doc_id, **asdict(s)}, ensure_ascii=False) + "\n"
                    )

    @classmethod
    def load_from_jsonl(cls, in_dir: str | Path) -> "DocumentStore":
        """Reconstruct a store from the output of ``to_jsonl`` (no vectors)."""
        in_dir = Path(in_dir)
        store = cls()
        for line in (in_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines():
            store.add_chunk(Chunk(**json.loads(line)))
        sections_file = in_dir / "sections.jsonl"
        if sections_file.exists():
            for line in sections_file.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                store.add_section(rec.pop("doc_id"), Section(**rec))
        return store
