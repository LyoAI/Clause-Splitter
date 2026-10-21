"""Embedding producers for the faiss searcher.

``Embedder`` wraps a sentence-transformers model with two encode methods; it is
the only place an embedding model is loaded. ``embed_store`` turns a store's
chunks into a persistent faiss index (the "vector database") so the expensive
embedding step runs once per corpus. The searcher then reads the index while
text always comes from the store (single source of truth).

This is the ``embed`` stage of the ingestion pipeline. The upstream ``parse``
(PDF → blocks) and ``chunk`` (blocks → units) stages are defined as interfaces
in ``chillrag.tools.parser``.
"""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

import numpy as np

from chillrag.config import EMBED_BATCH_SIZE, EMBED_MODEL, doc_cache_dir
from chillrag.utils import get_logger
from .store import DocumentStore

logger = get_logger()


class Embedder:
    """Minimal sentence-transformers wrapper: encode batch + query.

    The model import is lazy so the rest of the tools layer (grep / bm25 /
    searchers) never requires sentence-transformers to be installed.
    """

    def __init__(
        self,
        model_name: str = EMBED_MODEL,
        device: str | None = None,
        query_prefix: str = "",
    ) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.query_prefix = query_prefix
        self._model = SentenceTransformer(model_name, device=device)

    def encode_batch(
        self, texts: list[str], batch_size: int = EMBED_BATCH_SIZE
    ) -> np.ndarray:
        """Embed a list of texts; returns float32 ``(n, dim)``, L2-normalized."""
        if not texts:
            return np.empty(
                (0, self._model.get_sentence_embedding_dimension()), dtype=np.float32
            )
        return np.asarray(
            self._model.encode(
                texts,
                batch_size=batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
            ),
            dtype=np.float32,
        )

    def encode_query(self, text: str) -> np.ndarray:
        """Embed a single query; returns ``(dim,)``. Bound method usable directly
        as ``FaissSearcher(embed_query=...)``."""
        return self.encode_batch([self.query_prefix + text])[0]


def embed_store(
    store: DocumentStore,
    embedder,
    index_dir: str | Path | None = None,
    rebuild: bool = False,
) -> Path:
    """Embed all chunks and persist a flat faiss index (cosine similarity).

    Idempotent: when ``index_dir`` already holds ``index.faiss`` **and**
    ``vector_ids.json`` the cached index is reused without re-embedding (a
    half-written index is never treated as a hit). Corpus vectors are
    L2-normalized before storage, so inner-product search equals cosine.

    Args:
        store: The corpus store; vectors are computed from each chunk's text.
        embedder: Object with ``encode_batch(texts) -> (n, dim)`` — e.g. an
            ``Embedder``. Duck-typed so tests can stub it without a model.
        index_dir: Where to write the index. Defaults to the store's cache
            directory: ``.cache/<store.cache_key()>/faiss``.
        rebuild: Force a re-embed even when a cached index exists.

    Returns:
        ``index_dir``, ready to pass to ``FaissSearcher(index_dir=...)``.
    """
    index_dir = (
        Path(index_dir)
        if index_dir is not None
        else doc_cache_dir(store.cache_key()) / "faiss"
    )
    index_dir.mkdir(parents=True, exist_ok=True)
    index_file = index_dir / "index.faiss"
    ids_file = index_dir / "vector_ids.json"

    if rebuild or not (index_file.exists() and ids_file.exists()):
        import faiss  # heavy native lib — only on the re-embed path, not cache hits

        chunks = store.all_chunks()
        t0 = perf_counter()
        logger.info("Embedding %d chunks with %s", len(chunks), embedder.model_name)
        vectors = embedder.encode_batch([c.text for c in chunks])
        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(np.ascontiguousarray(vectors, dtype=np.float32))
        faiss.write_index(index, str(index_file))
        ids_file.write_text(
            json.dumps([c.chunk_id for c in chunks], ensure_ascii=False), encoding="utf-8"
        )
        logger.info(
            "Faiss index persisted (%d vectors, dim=%d) in %.1fs at %s",
            index.ntotal, index.d, perf_counter() - t0, index_dir,
        )
    else:
        logger.info("Reusing cached faiss index at %s", index_dir)
    return index_dir


def append_to_index(
    index_dir: str | Path,
    chunk_ids: list[str],
    vectors: np.ndarray,
) -> Path:
    """Add newly ingested chunks to an existing faiss index, in place.

    Appending preserves the row order the index already has — the stored
    ``vector_ids`` are extended, never reordered — so an incremental upload
    costs one embedding pass over the *new* chunks rather than a re-embed of the
    whole corpus.

    Writes ``index.faiss`` before ``vector_ids.json``. A crash between the two
    therefore leaves the id list short of the index, which ``FaissSearcher``
    detects on load and reports, rather than silently mis-attributing every row
    after the append point.

    Args:
        index_dir: Directory holding ``index.faiss`` + ``vector_ids.json``.
        chunk_ids: Ids of the appended chunks, aligned with ``vectors`` rows.
        vectors: ``(n, dim)`` L2-normalized embeddings, same dimension as the index.

    Returns:
        ``index_dir``.
    """
    import faiss

    index_dir = Path(index_dir)
    index_file = index_dir / "index.faiss"
    ids_file = index_dir / "vector_ids.json"
    if not (index_file.exists() and ids_file.exists()):
        logger.error("no dense index to append to at %s", index_dir)
        raise FileNotFoundError(
            f"no index to append to at {index_dir}; run embed_store first"
        )
    if not chunk_ids:
        return index_dir

    index = faiss.read_index(str(index_file))
    vectors = np.ascontiguousarray(vectors, dtype=np.float32)
    if vectors.shape[1] != index.d:
        logger.error(
            "embedding dimension mismatch: index has %d, new vectors have %d",
            index.d, vectors.shape[1],
        )
        raise ValueError(
            f"embedding dimension mismatch: index has {index.d}, new vectors have "
            f"{vectors.shape[1]} (is EMBED_MODEL the same as when the index was built?)"
        )
    index.add(vectors)
    faiss.write_index(index, str(index_file))
    ids = json.loads(ids_file.read_text(encoding="utf-8"))
    ids.extend(chunk_ids)
    ids_file.write_text(json.dumps(ids, ensure_ascii=False), encoding="utf-8")
    logger.info("Appended %d vectors to %s (now %d)", len(chunk_ids), index_dir, index.ntotal)
    return index_dir
