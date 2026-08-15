"""Embedding producers for the faiss searcher.

``Embedder`` wraps a sentence-transformers model with two encode methods; it is
the only place an embedding model is loaded. ``embed_store`` turns a store's
clause chunks into a persistent faiss index (the "vector database") so the
expensive embedding step runs once per corpus. The searcher then reads the
index while text always comes from the store (single source of truth).
"""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

import numpy as np

from clause_splitter.config import EMBED_BATCH_SIZE, EMBED_MODEL, doc_cache_dir
from clause_splitter.utils import get_logger
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
    append_new: bool = False,
) -> Path:
    """Embed all chunks and persist a flat faiss index (cosine similarity).

    Idempotent: when ``index_dir`` already holds ``index.faiss`` **and**
    ``vector_ids.json`` the cached index is reused without re-embedding (a
    half-written index is never treated as a hit). Corpus vectors are
    L2-normalized before storage, so inner-product search equals cosine.

    Incremental modes:
    - ``rebuild=True`` — full re-embed of every chunk.
    - ``append_new=True`` (and not rebuild) — only chunks whose id is missing
      from ``vector_ids.json`` are embedded and appended; existing rows stay.
      **Only safe when the change is a pure addition** (no modified / removed
      documents): a modified doc can shift its chunk ids, leaving stale rows
      that must be rebuilt, not appended to. The caller decides.

    Args:
        store: The corpus store; vectors are computed from each chunk's text.
        embedder: Object with ``encode_batch(texts) -> (n, dim)`` — e.g. an
            ``Embedder``. Duck-typed so tests can stub it without a model.
        index_dir: Where to write the index. Defaults to the store's per-corpus
            cache: ``.cache/<store.cache_key()>/faiss``.
        rebuild: Force a re-embed even when a cached index exists.
        append_new: Embed and append only new chunks (pure-addition updates).

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
        return index_dir

    if append_new:
        import faiss  # noqa: F401 — only on the append path

        existing_ids = json.loads(ids_file.read_text(encoding="utf-8"))
        known = set(existing_ids)
        new_chunks = [c for c in store.all_chunks() if c.chunk_id not in known]
        if not new_chunks:
            logger.info("Faiss index up-to-date (%d vectors) at %s", len(existing_ids), index_dir)
            return index_dir
        index = faiss.read_index(str(index_file))
        t0 = perf_counter()
        vectors = embedder.encode_batch([c.text for c in new_chunks])
        index.add(np.ascontiguousarray(vectors, dtype=np.float32))
        faiss.write_index(index, str(index_file))
        ids_file.write_text(
            json.dumps(existing_ids + [c.chunk_id for c in new_chunks], ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "Faiss index appended %d new vectors → %d total in %.1fs at %s",
            len(new_chunks), index.ntotal, perf_counter() - t0, index_dir,
        )
        return index_dir

    logger.info("Reusing cached faiss index at %s", index_dir)
    return index_dir
