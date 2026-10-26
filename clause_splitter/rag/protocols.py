"""The single retrieval interface the business code depends on."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .types import Chunk, CorpusStats, DocInfo, Hit, ParsedDoc, RagCapabilities


class ChunkLookup(Protocol):
    """Resolves chunk ids returned by retrieval in the current run."""

    def get_chunk(self, chunk_id: str) -> Chunk | None: ...


class RagService(Protocol):
    """External RAG service (parse / ingest / search / inspect)."""

    #: Label used in review candidates and episode metadata.
    name: str

    #: False when no endpoint is configured; business stages then fail early
    #: with a readable message instead of a stack trace.
    available: bool

    def capabilities(self) -> RagCapabilities: ...

    def parse(self, path: Path, doc_id: str) -> ParsedDoc:
        """Parse a source document into full text / markdown."""

    def ingest(self, path: Path, name: str, doc_id: str = "") -> DocInfo:
        """Upsert one internal document into the corpus."""

    def delete(self, doc_id: str) -> None:
        """Remove one document (and its chunks) from the corpus."""

    def search(self, query: str, k: int) -> list[Hit]:
        """Hybrid ranked retrieval."""

    def fetch_chunks(self, doc_id: str) -> list[Chunk]:
        """All chunks of one document in reading order."""

    def documents(self) -> list[DocInfo]:
        """List corpus documents."""

    def stats(self) -> CorpusStats:
        """Corpus counters plus an index version string."""
