"""Dense-vector searcher backed by FAISS.

The store owns the corpus text. Dense vectors come from one of two places:
- in-memory ``store.vectors`` + ``store.vector_ids`` (filled during ingestion),
- a persisted faiss index passed as ``index_dir`` (``index.faiss`` +
  ``vector_ids.json``, as written by ``embed_store``) — the store's vectors are
  then not required.

A real embedding model is never loaded here; ``embed_query`` is injected by the
caller (e.g. ``Embedder.encode_query``). Scores are cosine similarities because
query and corpus vectors are L2-normalized.
"""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Callable

import numpy as np

from chillrag.utils import get_logger
from .base import BaseSearcher, Hit
from .store import DocumentStore

logger = get_logger()


class FaissSearcher(BaseSearcher):
    """Cosine similarity search over precomputed chunk embeddings."""

    name = "faiss"
    description = (
        "Semantic (dense vector) search over the corpus. Use it when keyword "
        "search misses because the answer is phrased differently from the query "
        "(paraphrase, synonyms). Returns the top-k chunk snippets."
    )

    def __init__(
        self,
        store: DocumentStore,
        embed_query: Callable[[str], np.ndarray],
        normalize: bool = True,
        index_dir: str | Path | None = None,
    ) -> None:
        """Build an index from ``store.vectors`` or load one from ``index_dir``.

        Args:
            store: The corpus store; text is always read back from here.
            embed_query: Embedding callable (e.g. ``Embedder.encode_query``).
            normalize: L2-normalize in-memory corpus vectors. Vectors persisted
                by ``embed_store`` are already normalized, so this is a no-op
                on the ``index_dir`` path. Queries are always normalized.
            index_dir: A persisted faiss index (``index.faiss`` +
                ``vector_ids.json``, as written by ``embed_store``). When given,
                ``store.vectors`` is not required.
        """
        self.store = store
        self._embed_query = embed_query
        # Lazy import: faiss is a heavy native lib; only load it when a faiss
        # searcher is actually constructed.
        import faiss

        if index_dir is not None:
            index_dir = Path(index_dir)
            self._index = faiss.read_index(str(index_dir / "index.faiss"))
            self._vector_ids = json.loads(
                (index_dir / "vector_ids.json").read_text(encoding="utf-8")
            )
            # Row ``i`` of the index must be row ``i`` of the id list. An
            # incremental append writes the index before the ids, so a crash
            # between them shows up here — fail loudly instead of attributing
            # every later hit to the wrong chunk.
            if self._index.ntotal != len(self._vector_ids):
                logger.error(
                    "corrupt faiss index at %s: %d vectors, %d ids",
                    index_dir, self._index.ntotal, len(self._vector_ids),
                )
                raise RuntimeError(
                    f"corrupt faiss index at {index_dir}: {self._index.ntotal} vectors "
                    f"but {len(self._vector_ids)} ids — rebuild the index"
                )
        else:
            if store.vectors is None or not store.vector_ids:
                logger.error("corpus %r has no vectors and no index_dir", store.name)
                raise ValueError(
                    "FaissSearcher needs store.vectors and store.vector_ids (fill them "
                    "during ingestion), or pass index_dir= to read a persisted index."
                )
            # Copy so normalize_L2 never mutates the store's array.
            vectors = np.ascontiguousarray(store.vectors, dtype=np.float32).copy()
            if normalize:
                faiss.normalize_L2(vectors)
            self._index = faiss.IndexFlatIP(vectors.shape[1])
            self._index.add(vectors)
            self._vector_ids = list(store.vector_ids)

    def search(self, query: str, k: int) -> list[Hit]:
        if k <= 0:
            return []
        import faiss

        t0 = perf_counter()
        q = np.asarray(self._embed_query(query), dtype=np.float32).reshape(1, -1)
        faiss.normalize_L2(q)
        distances, indices = self._index.search(q, k)
        hits: list[Hit] = []
        for dist, i in zip(distances[0], indices[0]):
            if i < 0:  # faiss pads with -1 when fewer than k vectors exist
                break
            chunk = self.store.get_chunk(self._vector_ids[i])
            if chunk is None:
                # The index names a chunk the store does not hold — stale index.
                logger.warning("faiss hit %r is not in the store", self._vector_ids[i])
                continue
            hits.append(Hit(score=float(dist), chunk_id=chunk.chunk_id, text=chunk.text, meta=chunk.with_doc()))
        logger.info(
            "faiss_search(query=%r, k=%d) -> %d hits in %.1fms",
            query, k, len(hits), (perf_counter() - t0) * 1000,
        )
        return hits
