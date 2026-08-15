"""Offline ingestion: documents → chunks → on-disk corpus store.

``ingest`` runs the offline pipeline for one batch of documents — parse (local
MinerU for pdf/docx/pptx/images, ``split_workbook`` for Excel multi-regulation
containers) → blocks → hierarchy-aware chunks + table of contents →
DocumentStore — then persists the store (JSONL) and materializes per-chunk text
files for the grep searcher. Embedding / index building are later stages that
read the same store.

Chunk granularity is **unified per-条**: every 第X条 opens its own retrieval
unit (a ``title`` block with ``level <= CHUNK_LEVEL``), with the chapter path
kept as ``meta.section`` context (e.g. "第一章　总则 > 第一条"). An Excel
workbook with a 制度名称 column becomes one document per 制度, each chunked
per-条 with the 条号 as ``meta.section``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

from clause_splitter.config import MINERU_MD_DIRNAME, doc_cache_dir
from clause_splitter.utils import get_logger

from ..searcher.store import DocumentStore
from .chunker import chunk_doc, doc_sections
from .containers import split_workbook
from .headings import infer_regulation_headings
from .mineru_parse import SUPPORTED_SUFFIXES, parse_docs

logger = get_logger()


def scan_supported_docs(internal_dir: str | Path) -> list[Path]:
    """Every supported document under ``internal_dir``, sorted by filename."""
    internal_dir = Path(internal_dir)
    if not internal_dir.is_dir():
        raise FileNotFoundError(f"no such internal-library directory: {internal_dir}")
    for p in sorted(internal_dir.iterdir()):
        if p.is_file() and p.suffix.lower() == ".xls":
            logger.warning(
                "检测到旧版 .xls: %s — openpyxl 无法读取,请转换为 .xlsx 后再索引", p.name
            )
    docs = sorted(
        p for p in internal_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )
    return docs


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _dedup_doc_id(blocks_map: dict[str, object], doc_id: str, source_file: str) -> str:
    """Uniquify ``doc_id`` on cross-source collision (same 制度名 in two files).

    Prefixes the colliding doc with the source file's stem, deterministically
    (sources are processed in sorted order), so incremental updates see a
    stable doc_id for a given source set.
    """
    if doc_id not in blocks_map:
        return doc_id
    stem = Path(source_file).stem
    candidate, n = f"{stem}/{doc_id}", 1
    while candidate in blocks_map:
        n += 1
        candidate = f"{stem}-{n}/{doc_id}"
    logger.warning("制度名冲突,已加源文件前缀去重: %s → %s", doc_id, candidate)
    return candidate


def ingest(
    doc_paths: Iterable[Path],
    name: str = "internal",
    *,
    persist: bool = True,
    index_root: str | Path | None = None,
) -> DocumentStore:
    """Parse + chunk ``doc_paths`` into a DocumentStore.

    Args:
        doc_paths: Documents to ingest — pdf/image/docx/pptx go through MinerU;
            ``.xlsx`` is treated as a multi-regulation container (per-制度 docs).
        name: Store / cache identity (``<root>/<name>/``).
        persist: Write the store (JSONL) and materialize chunk files under the
            cache dir.
        index_root: Cache parent override (default ``INDEX_CACHE_DIR`` env var,
            then ``CACHE_DIR``). Persisted in ``store/meta.json`` so later
            compare/update runs locate the same indexes.

    Returns:
        The populated store (chunks in reading order) with ``store.sources``
        filled with per-doc provenance (source file + sha256 + kind).
    """
    paths = sorted((Path(p) for p in doc_paths), key=str)
    if not paths:
        raise ValueError("no documents to ingest")

    cache_dir = doc_cache_dir(name, root=index_root)
    workbooks = [p for p in paths if p.suffix.lower() == ".xlsx"]
    regular = [p for p in paths if p.suffix.lower() != ".xlsx"]

    blocks_map: dict[str, list[object]] = {}
    sources: dict[str, dict] = {}

    if regular:
        path_by_name = {p.name: p for p in regular}
        for doc_id, blocks in parse_docs(regular, out_dir=cache_dir / MINERU_MD_DIRNAME).items():
            src = path_by_name.get(doc_id)
            blocks_map[doc_id] = blocks
            sources[doc_id] = {
                "path": str(src) if src else "",
                "sha256": _file_sha256(src) if src else "",
                "kind": "file",
                "source_file": str(src) if src else "",
            }

    for wb in workbooks:
        for reg in split_workbook(wb):
            doc_id = _dedup_doc_id(blocks_map, reg.doc_id, reg.source_file)
            blocks_map[doc_id] = reg.blocks
            sources[doc_id] = {
                "path": "",
                "sha256": _file_sha256(wb),
                "kind": "excel_regulation",
                "source_file": reg.source_file,
            }

    store = DocumentStore(name=name)
    store.index_root = str(index_root) if index_root else ""
    for doc_id, blocks in blocks_map.items():
        # Hand-written regulations (bold 第X条, no heading styles) come out of
        # MinerU with no ``title`` blocks; re-classify their headings so the
        # chunker groups per-条 (container blocks are already per-条 and pass
        # through unchanged). Non-regulation documents are untouched.
        blocks = infer_regulation_headings(blocks)
        store.add_chunks(chunk_doc(doc_id, blocks))
        for section in doc_sections(doc_id, blocks):
            store.add_section(doc_id, section)
    store.sources = sources
    logger.info(
        "ingested %s: %d docs → %d chunks, %d toc sections",
        name, len(blocks_map), len(store.all_chunks()),
        sum(len(s) for s in store.sections.values()),
    )
    if persist:
        store.to_jsonl(cache_dir / "store")
        store.materialize(cache_dir / "chunks")
    return store


def ingest_internal(
    internal_dir: str | Path,
    name: str = "internal",
    *,
    persist: bool = True,
    index_root: str | Path | None = None,
) -> DocumentStore:
    """Ingest every supported document under ``internal_dir``."""
    docs = scan_supported_docs(internal_dir)
    if not docs:
        raise FileNotFoundError(f"no supported documents under {internal_dir}")
    return ingest(docs, name=name, persist=persist, index_root=index_root)
