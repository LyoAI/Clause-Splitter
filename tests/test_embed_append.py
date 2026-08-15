"""Tests for faiss index incremental modes (``embed_store``).

The correctness-critical decision lives in the *caller* (``prepare_docs``): a
pure-addition update may append new vectors to the existing index, but a
modified / removed document must force a full re-embed — appending would leave
stale rows whose chunk ids drift. These tests pin the append / reuse / rebuild
behavior of ``embed_store`` with a deterministic fake embedder (no model).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from clause_splitter.tools.searcher.embed import embed_store
from clause_splitter.tools.searcher.store import Chunk, DocumentStore


class FakeEmbedder:
    model_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        n = len(texts)
        return np.asarray([[1.0, 0.0, 0.0, 0.0]] * n, dtype=np.float32)


def _store(n: int) -> DocumentStore:
    store = DocumentStore(name="x")
    for i in range(n):
        store.add_chunk(Chunk(chunk_id=f"doc#{i}", doc_id="doc", order=i, text=f"内容 {i}"))
    return store


def _ids(index_dir: Path) -> list[str]:
    import json
    return json.loads((index_dir / "vector_ids.json").read_text(encoding="utf-8"))


def test_full_build_then_reuse(tmp_path: Path) -> None:
    store = _store(2)
    embedder = FakeEmbedder()
    index_dir = embed_store(store, embedder, index_dir=tmp_path / "faiss")
    assert _ids(index_dir) == ["doc#0", "doc#1"]
    assert len(embedder.calls) == 1  # one full encode

    embed_store(store, embedder, index_dir=index_dir)  # unchanged → reuse
    assert len(embedder.calls) == 1  # no new encode


def test_append_new_embeds_only_new_chunks(tmp_path: Path) -> None:
    store = _store(2)
    embedder = FakeEmbedder()
    index_dir = embed_store(store, embedder, index_dir=tmp_path / "faiss")

    store.add_chunk(Chunk(chunk_id="doc#2", doc_id="doc", order=2, text="新增"))
    embed_store(store, embedder, index_dir=index_dir, append_new=True)
    assert _ids(index_dir) == ["doc#0", "doc#1", "doc#2"]
    # only the new chunk was embedded on the append call
    assert embedder.calls[-1] == ["新增"]


def test_append_new_idempotent(tmp_path: Path) -> None:
    store = _store(3)
    embedder = FakeEmbedder()
    index_dir = embed_store(store, embedder, index_dir=tmp_path / "faiss")
    n_calls = len(embedder.calls)
    embed_store(store, embedder, index_dir=index_dir, append_new=True)  # all present
    assert len(embedder.calls) == n_calls  # nothing to append
    assert len(_ids(index_dir)) == 3


def test_rebuild_forces_full_reembed(tmp_path: Path) -> None:
    store = _store(2)
    embedder = FakeEmbedder()
    index_dir = embed_store(store, embedder, index_dir=tmp_path / "faiss")

    # a modified doc changes chunk ids; append would leave a stale "doc#1"
    store.docs["doc"] = [Chunk(chunk_id="doc#1", doc_id="doc", order=0, text="改了")]
    store.chunks_by_id = {c.chunk_id: c for c in store.all_chunks()}
    embed_store(store, embedder, index_dir=index_dir, rebuild=True)
    assert _ids(index_dir) == ["doc#1"]
    assert len(embedder.calls) == 2  # full rebuild encode
