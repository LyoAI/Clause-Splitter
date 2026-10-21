"""Internal-library preparation — MinerU parse + per-条 chunking → indexes.

Input is the raw internal-regulation documents (pdf / docx / pptx / xlsx
containers / images):

    parse  local MinerU CLI → blocks (.xlsx goes through ``split_workbook``,
           one document per 制度)
    chunk  hierarchy-aware chunking → retrieval units (unified per-article:
           one chunk per 第X条, chapter path in meta.section)
    store  DocumentStore → JSONL (``store/``) + per-chunk .txt (``chunks/``)
           + source manifest (``sources.json``)
    embed  chunks → faiss vector index (``faiss/``)
    index  chunks → BM25 Lucene index (``bm25/``)

**Incremental updates**: ``prepare_docs`` reads the previous
``store/sources.json`` and diffs the directory file-by-file by sha256 into
added / modified / unchanged / removed:

- Only added+modified files are re-parsed (MinerU ``skip_existing`` reuses the
  unchanged md cache; modified files have their stale cache removed first).
  Re-chunking covers all current documents — cheap, no LLM/OCR.
- faiss: pure additions → embed only the new chunks and ``append_to_index``;
  any modification/removal → full re-embed.
- bm25: any change → rebuild from the full store (~1s); else reuse the cache.
- ``rebuild=True`` forces a full rebuild (drop all md caches + re-embed).

**Custom storage location**: ``index_root`` (or ``INDEX_CACHE_DIR``) decides
``<root>/<name>/{store,chunks,bm25,faiss,md}``; ``store/meta.json`` records
``index_root``, and ``load_store`` lets the compare side mount an existing
index without re-running MinerU.

Embedding / BM25 primitives come from the vendored chillrag engine; the
sha256-based incremental diff is this module's business policy.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from chillrag.config import EMBED_DEVICE, EMBED_MODEL
from chillrag.tools.searcher import DocumentStore, append_to_index, embed_store
from chillrag.tools.searcher.bm25_search import BM25Searcher
from chillrag.tools.searcher.embed import Embedder

from clause_splitter.config import MINERU_MD_DIRNAME, doc_cache_dir
from clause_splitter.docprep.ingest import _file_sha256, ingest, scan_supported_docs
from clause_splitter.utils import get_logger

logger = get_logger()


def _scan_sources(internal_dir: str | Path) -> dict[Path, str]:
    """{source file: sha256} for every supported document under ``internal_dir``."""
    return {p: _file_sha256(p) for p in scan_supported_docs(internal_dir)}


def _load_sources(cache_root: Path) -> dict[str, dict]:
    """The persisted ``store/sources.json``, or {} when absent / unreadable."""
    f = cache_root / "store" / "sources.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt manifest is treated as empty
            logger.warning("sources.json unreadable, treating as empty: %s", f)
    return {}


def _refresh_faiss(
    store: DocumentStore,
    cache_root: Path,
    *,
    rebuild: bool,
    modified: list[Path],
    removed: list[str],
    embed_model: str | None,
) -> None:
    """faiss incremental policy: pure additions append only the new chunks;
    any modification/removal (or a forced rebuild) re-embeds everything."""
    faiss_dir = cache_root / "faiss"
    reembed = rebuild or bool(modified or removed)
    try:
        embedder = Embedder(model_name=embed_model or EMBED_MODEL, device=EMBED_DEVICE)
        if reembed or not (faiss_dir / "index.faiss").exists():
            embed_store(store, embedder, index_dir=faiss_dir, rebuild=reembed)
            return
        # Pure addition: embed only chunks missing from vector_ids, append in
        # place (no full-corpus re-embed).
        ids_file = faiss_dir / "vector_ids.json"
        existing = set(json.loads(ids_file.read_text(encoding="utf-8")))
        new_chunks = [c for c in store.all_chunks() if c.chunk_id not in existing]
        if not new_chunks:
            logger.info("no new chunks for the faiss index, skipping")
            return
        logger.info("faiss incremental: appending %d new chunk(s)", len(new_chunks))
        vectors = embedder.encode_batch([c.text for c in new_chunks])
        append_to_index(faiss_dir, [c.chunk_id for c in new_chunks], vectors)
    except Exception as e:  # noqa: BLE001 — a missing model must not block grep/bm25
        logger.warning("faiss index skipped (embedding unavailable): %s", e)


def prepare_docs(
    internal_dir: str | Path,
    *,
    name: str = "internal",
    embed_model: str | None = None,
    rebuild: bool = False,
    persist: bool = True,
    index_root: str | Path | None = None,
) -> DocumentStore:
    """Run the offline pipeline for the internal library and return the store.

    Idempotent and incremental: unchanged source files reuse their cached
    MinerU parse / faiss rows; only added / modified files are re-parsed.
    ``rebuild=True`` forces a full rebuild (re-parse + re-embed + re-index).

    Args:
        internal_dir: Directory of internal-regulation documents
            (pdf / docx / pptx / xlsx containers / images).
        name: Store / cache identity (``<root>/<name>/``).
        embed_model: Embedding model path; default ``EMBED_MODEL``.
        rebuild: Force-rebuild all derived indexes.
        persist: Gate every disk write — False runs parse → chunk → store in
            memory only and builds no indexes.
        index_root: Cache parent override (default ``INDEX_CACHE_DIR`` env var,
            then ``CACHE_DIR``).

    Returns:
        The populated store (chunks in reading order).
    """
    cache_root = doc_cache_dir(name, root=index_root)
    current = _scan_sources(internal_dir)
    prev = _load_sources(cache_root)

    if rebuild:
        md_dir = cache_root / MINERU_MD_DIRNAME
        if md_dir.exists():
            logger.info("--rebuild: dropping all MinerU parse caches %s", md_dir)
            shutil.rmtree(md_dir, ignore_errors=True)
        added = list(current)
        modified: list[Path] = []
        removed: list[str] = []
    else:
        prev_hash = {
            s.get("source_file"): s.get("sha256")
            for s in prev.values() if s.get("source_file")
        }
        added = [p for p in current if str(p) not in prev_hash]
        modified = [
            p for p in current
            if str(p) in prev_hash and prev_hash[str(p)] != current[p]
        ]
        removed = [f for f in prev_hash if f not in {str(p) for p in current}]
        # Changed files: drop their stale md cache so parse_docs(skip_existing)
        # re-parses them. Unchanged files reuse the cache directly — the
        # incremental run only pays MinerU for new/modified files.
        md_dir = cache_root / MINERU_MD_DIRNAME
        for p in modified:
            stale = md_dir / p.stem
            if stale.exists():
                logger.info("file changed, dropping stale parse cache: %s", p.name)
                shutil.rmtree(stale, ignore_errors=True)

    changed = bool(added or modified or removed)
    logger.info(
        "incremental diff %s: added=%d modified=%d removed=%d → changed=%s",
        name, len(added), len(modified), len(removed), changed,
    )

    if not persist:
        return ingest(list(current), name=name, persist=False, index_root=index_root)

    store = ingest(list(current), name=name, persist=True, index_root=index_root)

    _refresh_faiss(store, cache_root, rebuild=rebuild, modified=modified,
                   removed=removed, embed_model=embed_model)
    BM25Searcher(store, index_dir=cache_root / "bm25", rebuild=changed)
    return store


def load_store(
    name: str = "internal",
    index_root: str | Path | None = None,
) -> DocumentStore | None:
    """Load a previously built store + its persisted provenance, or None.

    Only the serialized store is read — no MinerU parse, no index rebuild — so
    the comparison stage can reuse an existing internal-library index directly.
    The store's ``index_root`` comes from ``store/meta.json``, keeping the
    faiss / bm25 / chunks locations aligned with whichever root built it.

    Args:
        name: Store identity (``<root>/<name>/``).
        index_root: Where to look for the store; default ``CACHE_DIR`` /
            ``INDEX_CACHE_DIR``. If a store exists there, its persisted
            ``index_root`` wins for locating the derived indexes.
    """
    cache_root = doc_cache_dir(name, root=index_root)
    store_dir = cache_root / "store"
    if not (store_dir / "chunks.jsonl").exists():
        return None
    store = DocumentStore.load_from_jsonl(store_dir)
    logger.info("loaded existing internal index %s (%d docs / %d units)",
                name, len(store.docs), len(store.all_chunks()))
    return store
