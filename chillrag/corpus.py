"""Process-wide corpus manager: one loaded corpus per folder, shared by callers.

Retrieval requests are frequent and cheap; loading a corpus is neither. This is
the single owner of the expensive objects — the parsed store, the retrieval
indexes, and the embedding model — which buys three things:

- **Cheap handles.** ``RAGTool()`` is built per request and stays a bare
  attribute holder, while the corpus behind it loads once per process.
- **One embedding model.** A model per corpus multiplies device memory by the
  number of folders and ends in an OOM; there is exactly one (``get_embedder``).
- **Distrust of the cache.** Each bundle records a fingerprint of its source
  documents, so editing, adding or removing one invalidates the corpus rather
  than silently serving stale text.

The cache layout mirrors the source layout: ``<materials>/<folder>/`` holds
documents, ``<cache>/<folder>/{md,store,chunks,faiss,bm25}/`` holds everything
derived from them. A folder is the unit of both identity and invalidation.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Iterable

from chillrag.config import CACHE_DIR, EMBED_DEVICE, MATERIALS_DIR, MINERU_MD_DIRNAME, SEARCHER_TYPE
from chillrag.utils import get_logger

from .tools.parser import SOURCE_SUFFIXES, chunk_doc, doc_sections, parse_sources
from .tools.parser.blocks import Block
from .tools.searcher.bm25_search import _has_segments
from .tools.searcher import (
    BaseSearcher,
    BM25Searcher,
    Embedder,
    FaissSearcher,
    GrepSearcher,
    SearcherType,
    append_to_index,
    embed_store,
    searcher_kinds,
)
from .tools.searcher.store import DocumentStore

logger = get_logger()

# Subdirectories of a folder's cache directory.
_MD_DIRNAME = MINERU_MD_DIRNAME
_STORE_DIRNAME = "store"
_CHUNKS_DIRNAME = "chunks"
_FAISS_DIRNAME = "faiss"
_BM25_DIRNAME = "bm25"


# --------------------------------------------------------------- embedder --


_embedder_lock = threading.Lock()
_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """The process-wide embedding model, loaded on first use.

    Deliberately module-level rather than per-corpus: a second model instance
    costs another copy of the weights on the device, and the cost compounds with
    every folder a process touches.
    """
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            logger.info("loading embedding model on %s (first use)", EMBED_DEVICE)
            _embedder = Embedder(device=EMBED_DEVICE)
            logger.info("embedding model ready: %s", _embedder.model_name)
        return _embedder


# ---------------------------------------------------------------- bundle --


@dataclass
class CorpusBundle:
    """A folder's loaded corpus plus the retrieval backends built over it.

    Searchers are built on demand and cached here: each one has a real startup
    cost (Lucene boots a JVM, faiss loads the model, grep needs the chunk files)
    and a caller that only ever uses dense retrieval should not pay for the
    others.
    """

    folder: str
    store: DocumentStore
    signature: str
    cache_dir: Path
    searchers: dict[str, BaseSearcher] = field(default_factory=dict)

    def chunks_of(self, doc_id: str):
        """The document's chunks in reading order (empty if unknown)."""
        return self.store.docs.get(doc_id, [])

    def doc_ids(self) -> list[str]:
        return sorted(self.store.docs)


# --------------------------------------------------------------- manager --


class CorpusManager:
    """Owns every loaded corpus in the process, keyed by folder.

    Use ``CorpusManager.instance()`` for the shared one. Constructing a
    separate manager with explicit roots is supported for tests and for
    one-off tooling that must not disturb the shared cache.
    """

    _instance: "CorpusManager | None" = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "CorpusManager":
        """The process-wide manager."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self, materials_dir: str | Path | None = None,
                 cache_dir: str | Path | None = None) -> None:
        self._materials_dir = Path(materials_dir) if materials_dir else None
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._bundles: dict[str, CorpusBundle] = {}
        # Reentrant: a public method may call another while holding the lock.
        self._lock = threading.RLock()

    # ----------------------------------------------------------- layout --

    @property
    def materials_dir(self) -> Path:
        """Root of the source documents, one directory per folder."""
        return self._materials_dir or MATERIALS_DIR

    @property
    def cache_dir(self) -> Path:
        """Root of the derived artifacts, one directory per folder."""
        return self._cache_dir or CACHE_DIR

    def source_dir(self, folder: str) -> Path:
        return self.materials_dir / folder

    def cache_dir_for(self, folder: str) -> Path:
        return self.cache_dir / folder

    def source_docs(self, folder: str) -> list[Path]:
        """The folder's ingestable documents, sorted by name (empty if absent)."""
        directory = self.source_dir(folder)
        if not directory.is_dir():
            return []
        return sorted(
            p for p in directory.iterdir()
            if p.is_file()
            and p.suffix.lower() in SOURCE_SUFFIXES
            and not p.name.startswith(".")
        )

    def signature(self, folder: str) -> str:
        """Fingerprint of the folder's source documents: name, size, mtime.

        Recorded on every bundle so a changed folder is noticed instead of
        served from cache. Second-granularity mtime: a document replaced within
        the same second at an identical size is not detected — ``rebuild=True``
        is the escape hatch.
        """
        digest = hashlib.md5()
        for path in self.source_docs(folder):
            stat = path.stat()
            digest.update(f"{path.name}\0{stat.st_size}\0{int(stat.st_mtime)}\0".encode())
        return digest.hexdigest()[:16]

    def is_prepared(self, folder: str) -> bool:
        """True when the folder's store and dense index are on disk."""
        cache = self.cache_dir_for(folder)
        return (cache / _STORE_DIRNAME / "chunks.jsonl").is_file() and _has_dense_index(cache)

    # ------------------------------------------------------------- read --

    def get(self, folder: str, *, reload: bool = False) -> CorpusBundle:
        """The folder's corpus, loading it from cache on first use.

        Reloads when the folder's fingerprint no longer matches the loaded
        bundle, so a document added by someone else takes effect without a
        process restart.

        Raises:
            FileNotFoundError: the folder has never been prepared.
        """
        with self._lock:
            return self._get_locked(folder, reload=reload)

    def try_get(self, folder: str, *, reload: bool = False) -> CorpusBundle | None:
        """``get``, but never waits for the manager lock.

        ``prepare`` holds ``_lock`` across the whole parse and embed, and the
        lock is on the singleton rather than per folder — so while *any* folder
        is ingesting, ``get`` blocks for minutes even for a folder that is not
        the one ingesting. A caller that only wants to report, rather than to
        retrieve, is better served by an answer now.

        Returns:
            The bundle, or ``None`` when another thread holds the lock.

        Raises:
            FileNotFoundError: the folder has never been prepared. Raised only
                when the lock was free, so it is a real answer rather than a
                busy signal; ``None`` and this exception mean different things.
        """
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._get_locked(folder, reload=reload)
        finally:
            self._lock.release()

    def _get_locked(self, folder: str, *, reload: bool = False) -> CorpusBundle:
        """``get``'s body; the caller must hold ``_lock``."""
        signature = self.signature(folder)
        bundle = self._bundles.get(folder)
        if bundle is not None and not reload:
            if bundle.signature == signature:
                return bundle
            logger.info("corpus %r changed on disk; reloading", folder)
        loaded = self._load(folder, signature)
        self._bundles[folder] = loaded
        return loaded

    def find(self, folder: str) -> CorpusBundle | None:
        """The loaded bundle for ``folder``, or ``None`` if it is not loaded.

        Unlike ``get`` this never touches the disk, so callers that merely want
        to reuse an already-loaded corpus do not pay a stat walk for it.
        """
        with self._lock:
            return self._bundles.get(folder)

    def invalidate(self, folder: str) -> None:
        """Drop the loaded bundle so the next ``get`` re-reads it from disk."""
        with self._lock:
            self._bundles.pop(folder, None)

    def _load(self, folder: str, signature: str) -> CorpusBundle:
        cache = self.cache_dir_for(folder)
        store_file = cache / _STORE_DIRNAME / "chunks.jsonl"
        if not store_file.is_file():
            logger.error("no prepared corpus for folder %r at %s", folder, store_file)
            raise FileNotFoundError(
                f"folder {folder!r} has no prepared corpus at {store_file}; "
                f"call prepare({folder!r}) first"
            )
        t0 = perf_counter()
        store = DocumentStore.load_from_jsonl(cache / _STORE_DIRNAME)
        # ``to_jsonl`` drops the store's name, and every derived path hangs off
        # it — restore it before anything derives a path from this store.
        store.name = folder
        store.manifest = _read_manifest(cache / _CHUNKS_DIRNAME)
        logger.info(
            "loaded corpus %r: %d docs, %d chunks in %.2fs",
            folder, len(store.docs), len(store.all_chunks()), perf_counter() - t0,
        )
        return CorpusBundle(
            folder=folder, store=store, signature=signature, cache_dir=cache
        )

    # ----------------------------------------------------------- prepare --

    def ensure(self, folder: str, *, rebuild: bool = False) -> CorpusBundle:
        """The folder's corpus, preparing it first when it is not ready.

        The entry point callers should use: it is a no-op once a folder is both
        prepared and loaded, so it is safe to call on every upload. An
        already-loaded corpus whose fingerprint still matches the source folder
        is returned as-is — re-preparing would re-read every document's size and
        mtime for an answer already known.

        Anything else goes through ``prepare``, which is what notices documents
        added since the last run. Loading the cache directly would serve the
        old corpus forever: neither the disk cache nor the loaded bundle knows
        the source folder changed.
        """
        if not rebuild:
            bundle = self.find(folder)
            if bundle is not None and bundle.signature == self.signature(folder):
                return bundle
        return self.prepare(folder, rebuild=rebuild)

    def prepare(self, folder: str, *, rebuild: bool = False) -> CorpusBundle:
        """Parse, chunk, persist and index ``folder``; return the loaded corpus.

        Incremental by default: documents already in the store are not
        re-parsed (MinerU output is reused) or re-embedded, so adding one file
        to a large folder costs one file's work. ``rebuild=True`` discards the
        derived artifacts and redoes everything from the source documents.

        A document that disappeared from the source folder forces a full
        rebuild — an append-only update would otherwise keep serving text for a
        file that no longer exists.

        Raises:
            FileNotFoundError: the folder has no ingestable documents.
        """
        docs = self.source_docs(folder)
        if not docs:
            logger.error("no ingestable documents under %s", self.source_dir(folder))
            raise FileNotFoundError(
                f"no documents to ingest under {self.source_dir(folder)} "
                f"(supported: {', '.join(sorted(SOURCE_SUFFIXES))})"
            )
        cache = self.cache_dir_for(folder)
        signature = self.signature(folder)

        with self._lock:
            previous = None if rebuild else self._cached_store(folder)
            if previous is not None and set(previous.docs) - {d.name for d in docs}:
                logger.info(
                    "corpus %r lost documents that are still in the store; "
                    "rebuilding from scratch", folder,
                )
                previous = None

            if previous is None:
                store = self._build_store(folder, docs, cache)
                new_chunks = store.all_chunks()
            else:
                new_docs = [d for d in docs if d.name not in previous.docs]
                if not new_docs:
                    logger.info("corpus %r is up to date; nothing to ingest", folder)
                    self._ensure_indexes(previous, cache)
                    bundle = self._load(folder, signature)
                    self._bundles[folder] = bundle
                    return bundle
                store = previous
                new_chunks = self._append_store(store, folder, new_docs, cache)

            self._persist(store, cache)
            self._index(store, cache, new_chunks, rebuild=previous is None or rebuild)

            bundle = self._load(folder, signature)
            self._bundles[folder] = bundle
            return bundle

    # -------------------------------------------------------- build steps --

    def _cached_store(self, folder: str) -> DocumentStore | None:
        """The folder's current store, from memory or from disk (``None`` if absent)."""
        bundle = self.find(folder)
        if bundle is not None:
            return bundle.store
        if (self.cache_dir_for(folder) / _STORE_DIRNAME / "chunks.jsonl").is_file():
            return self._load(folder, self.signature(folder)).store
        return None

    def _build_store(self, folder: str, docs: list[Path], cache: Path) -> DocumentStore:
        """Parse + chunk every document in ``docs`` into a fresh store."""
        blocks_map = parse_sources(docs, out_dir=cache / _MD_DIRNAME)
        store = DocumentStore(name=folder)
        for doc_id, blocks in blocks_map.items():
            _add_document(store, doc_id, blocks)
        logger.info(
            "ingested %r: %d docs -> %d chunks", folder, len(blocks_map), len(store.all_chunks())
        )
        return store

    def _append_store(
        self, store: DocumentStore, folder: str, docs: list[Path], cache: Path
    ) -> list:
        """Parse ``docs`` and append their chunks to ``store``; return the new chunks."""
        blocks_map = parse_sources(docs, out_dir=cache / _MD_DIRNAME)
        added = []
        for doc_id, blocks in blocks_map.items():
            added.extend(_add_document(store, doc_id, blocks))
        logger.info(
            "appended %d docs (%d chunks) to %r", len(blocks_map), len(added), folder
        )
        return added

    def _persist(self, store: DocumentStore, cache: Path) -> None:
        """Write the store as JSONL and materialize one text file per chunk."""
        store.to_jsonl(cache / _STORE_DIRNAME)
        store.materialize(cache / _CHUNKS_DIRNAME)

    def _index(
        self, store: DocumentStore, cache: Path, new_chunks: list, *, rebuild: bool
    ) -> None:
        """Build the dense index and the BM25 index over ``store``.

        Both are built here, at ingest, rather than left to whichever backend
        the agent happens to bind: ``SEARCHER_TYPE`` names the search *tools* the
        model may choose between, and has no business deciding what a finished
        corpus contains. A deployment that later widens the tool set, or that
        reads BM25 directly, expects the index to be there already.

        The dense index is appended to when possible (only ``new_chunks`` are
        embedded); BM25 is always rebuilt, being a pure inverted index over text
        with no model to re-run. A backend that is not installed is skipped with
        a warning rather than failing the whole ingest.
        """
        faiss_dir = cache / _FAISS_DIRNAME
        if rebuild or not _has_dense_index(cache):
            embed_store(store, get_embedder(), index_dir=faiss_dir, rebuild=True)
        elif new_chunks:
            # Incremental: the index already covers every previously ingested
            # chunk in ``all_chunks()`` order, and the new chunks are that
            # order's tail, so only they need embedding.
            logger.info("embedding %d new chunk(s) for %r", len(new_chunks), store.name)
            vectors = get_embedder().encode_batch([c.text for c in new_chunks])
            append_to_index(faiss_dir, [c.chunk_id for c in new_chunks], vectors)

        self._build_bm25(store, cache)

    def _ensure_indexes(self, store: DocumentStore, cache: Path) -> None:
        """Build whichever index is absent from disk.

        Reached when the store is already current — which is also the state a
        cache written by an earlier build is in, one that predates an index we
        now consider part of a finished corpus. Without this check such a corpus
        short-circuits as "up to date" forever and never acquires it.
        """
        if not _has_dense_index(cache):
            logger.info("dense index missing for %r; building it", store.name)
            embed_store(store, get_embedder(), index_dir=cache / _FAISS_DIRNAME, rebuild=True)
        # A committed Lucene index has segments_*; the empty bm25/ a failed
        # build leaves behind must count as absent, or the failure is permanent.
        if not _has_segments(cache / _BM25_DIRNAME):
            logger.info("BM25 index missing for %r; building it", store.name)
            self._build_bm25(store, cache)

    def _build_bm25(self, store: DocumentStore, cache: Path) -> None:
        """Rebuild the folder's BM25 index; a missing backend is not fatal.

        The dense path still answers without it, so an absent pyserini or JVM
        degrades retrieval rather than failing the ingest.
        """
        try:
            BM25Searcher(store, index_dir=cache / _BM25_DIRNAME, rebuild=True)
        except Exception as e:
            logger.warning("BM25 index not built for %r: %s", store.name, e)

    # --------------------------------------------------------- searchers --

    def searchers(self, folder: str, kinds: Iterable[str] | None = None) -> dict[str, BaseSearcher]:
        """The requested backends for ``folder``, keyed by name.

        Backends that cannot be built (a missing optional dependency, a missing
        JVM) are omitted with a warning: a corpus remains retrievable through
        whatever is available rather than failing outright.
        """
        kinds = list(kinds) if kinds is not None else _enabled_kinds()
        out: dict[str, BaseSearcher] = {}
        for kind in kinds:
            searcher = self.searcher(folder, kind)
            if searcher is not None:
                out[kind] = searcher
        if not out:
            logger.warning(
                "no retrieval backend available for %r; answering has no tools", folder
            )
        return out

    def searcher(self, folder: str, kind: str) -> BaseSearcher | None:
        """One backend for ``folder``, built on first use and cached in the bundle."""
        bundle = self.get(folder)
        cached = bundle.searchers.get(kind)
        if cached is not None:
            return cached
        try:
            built = self._build_searcher(bundle, kind)
        except Exception as e:
            logger.warning("backend %r unavailable for %r: %s", kind, folder, e)
            return None
        logger.info("built %r backend for %r", kind, folder)
        bundle.searchers[kind] = built
        return built

    def _build_searcher(self, bundle: CorpusBundle, kind: str) -> BaseSearcher:
        if kind == SearcherType.GREP.value:
            return GrepSearcher(bundle.store, bundle.cache_dir / _CHUNKS_DIRNAME)
        if kind == SearcherType.BM25.value:
            return BM25Searcher(bundle.store, index_dir=bundle.cache_dir / _BM25_DIRNAME)
        if kind == SearcherType.FAISS.value:
            return FaissSearcher(
                bundle.store,
                get_embedder().encode_query,
                index_dir=bundle.cache_dir / _FAISS_DIRNAME,
            )
        logger.error("unknown searcher backend %r", kind)
        raise ValueError(f"unknown searcher backend {kind!r}")


# --------------------------------------------------------------- helpers --


def _enabled_kinds() -> list[str]:
    """The backends to build by default, from ``SEARCHER_TYPE``."""
    return searcher_kinds(SEARCHER_TYPE)


def _has_dense_index(cache: Path) -> bool:
    """A dense index counts as present only when the id list is beside it.

    ``embed_store`` writes ``index.faiss`` then ``vector_ids.json``; a crash
    between the two leaves a row count with no ids, which ``FaissSearcher``
    rejects on load. Treating that as absent makes the next ingest rebuild it.
    """
    faiss_dir = cache / _FAISS_DIRNAME
    return (faiss_dir / "index.faiss").is_file() and (faiss_dir / "vector_ids.json").is_file()


def _add_document(store: DocumentStore, doc_id: str, blocks: list[Block]) -> list:
    """Chunk ``blocks`` into ``store`` and record the document's TOC."""
    chunks = chunk_doc(doc_id, blocks)
    store.add_chunks(chunks)
    for section in doc_sections(doc_id, blocks):
        store.add_section(doc_id, section)
    return chunks


def _read_manifest(chunks_dir: Path) -> dict[str, str]:
    """The ``materialize`` manifest (``filename -> chunk_id``), or ``{}``.

    Reused rather than regenerated: materializing is what produced these files,
    so the mapping on disk is already correct and rewriting it would only cost
    one write per chunk.
    """
    manifest_file = chunks_dir / "manifest.json"
    if not manifest_file.is_file():
        return {}
    try:
        return json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("unreadable chunk manifest at %s: %s", manifest_file, e)
        return {}
