"""Types exchanged with a RAG provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    """One immutable retrieval unit."""

    chunk_id: str
    doc_id: str
    order: int
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def with_doc(self) -> dict[str, Any]:
        """Provenance dict including the document name (tool output shape)."""
        return {**self.meta, "doc": self.doc_id}


@dataclass
class Hit:
    """One ranked search result."""

    chunk_id: str
    text: str
    score: float | None = None
    doc_id: str = ""
    section: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_chunk(self) -> Chunk:
        """Materialize the hit as a chunk (order falls back to the id suffix)."""
        order = 0
        if "#" in self.chunk_id:
            tail = self.chunk_id.rsplit("#", 1)[-1]
            if tail.isdigit():
                order = int(tail)
        meta = dict(self.meta)
        if self.section:
            meta.setdefault("section", self.section)
        return Chunk(
            chunk_id=self.chunk_id,
            doc_id=self.doc_id or self.chunk_id.split("#", 1)[0],
            order=order,
            text=self.text,
            meta=meta,
        )


@dataclass
class ParsedDoc:
    """Parsed document as returned by a provider."""

    doc_id: str
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocInfo:
    """One document in the internal corpus."""

    doc_id: str
    name: str = ""
    chunk_count: int = 0
    state: str = ""  # indexed | indexing | failed


@dataclass
class CorpusStats:
    """Corpus-level counters and the index version used for snapshots."""

    doc_count: int = 0
    chunk_count: int = 0
    version: str = ""
