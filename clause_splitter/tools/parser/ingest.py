"""Offline ingestion: documents → chunks → on-disk corpus store.

``ingest`` runs the offline pipeline for one batch of documents — parse (local
MinerU) → blocks → hierarchy-aware chunks + table of contents → DocumentStore —
then persists the store (JSONL) and materializes per-chunk text files for the
grep searcher. Embedding / index building are later stages that read the same
store.

Chunk granularity is deliberately *coarse*: the chunker groups a document by
heading hierarchy (a ``title`` block with ``level <= CHUNK_LEVEL`` opens a new
chunk, deeper sub-headings stay inside), so an internal-regulation corpus is
indexed at chapter/section granularity rather than per-clause — matching the
intent that comparison retrieval units are not overly fine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from clause_splitter.config import MINERU_MD_DIRNAME, doc_cache_dir
from clause_splitter.utils import get_logger

from ..searcher.store import DocumentStore
from .chunker import chunk_doc, doc_sections
from .headings import infer_regulation_headings
from .mineru_parse import SUPPORTED_SUFFIXES, parse_docs

logger = get_logger()


def scan_supported_docs(internal_dir: str | Path) -> list[Path]:
    """Every supported document under ``internal_dir``, sorted by filename."""
    internal_dir = Path(internal_dir)
    if not internal_dir.is_dir():
        raise FileNotFoundError(f"no such internal-library directory: {internal_dir}")
    docs = sorted(
        p for p in internal_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )
    return docs


def ingest(
    doc_paths: Iterable[Path],
    name: str = "internal",
    *,
    persist: bool = True,
) -> DocumentStore:
    """Parse + chunk ``doc_paths`` into a DocumentStore.

    Args:
        doc_paths: Documents to ingest (pdf / image / docx / pptx / xlsx).
        name: Store / cache identity (``.cache/<name>/``).
        persist: Write the store (JSONL) and materialize chunk files under
            ``.cache/<name>/``.

    Returns:
        The populated store (chunks in reading order).
    """
    paths = [Path(p) for p in doc_paths]
    if not paths:
        raise ValueError("no documents to ingest")
    blocks_map = parse_docs(paths, out_dir=doc_cache_dir(name) / MINERU_MD_DIRNAME)
    store = DocumentStore(name=name)
    for doc_id, blocks in blocks_map.items():
        # Hand-written regulations (bold 第X条, no heading styles) come out of
        # MinerU with no ``title`` blocks; re-classify their headings so the
        # chunker groups by chapter / section instead of collapsing the doc.
        blocks = infer_regulation_headings(blocks)
        store.add_chunks(chunk_doc(doc_id, blocks))
        for section in doc_sections(doc_id, blocks):
            store.add_section(doc_id, section)
    logger.info(
        "ingested %s: %d docs → %d chunks, %d toc sections",
        name, len(blocks_map), len(store.all_chunks()),
        sum(len(s) for s in store.sections.values()),
    )
    if persist:
        cache_dir = doc_cache_dir(name)
        store.to_jsonl(cache_dir / "store")
        store.materialize(cache_dir / "chunks")
    return store


def ingest_internal(internal_dir: str | Path, name: str = "internal", *, persist: bool = True) -> DocumentStore:
    """Ingest every supported document under ``internal_dir``."""
    docs = scan_supported_docs(internal_dir)
    if not docs:
        raise FileNotFoundError(f"no supported documents under {internal_dir}")
    return ingest(docs, name=name, persist=persist)
