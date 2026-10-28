"""The RAG interface business code depends on.

Implementations (ChillRAG today, the intranet service later) subclass
:class:`RagService` and keep every wire detail to themselves, so switching is a
config change: ``RAG_PROVIDER``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Protocol

from .types import Chunk, CorpusStats, DocInfo, Hit, ParsedDoc


class ChunkLookup(Protocol):
    """Read access to chunks already seen during one comparison run."""

    def get_chunk(self, chunk_id: str) -> Chunk | None: ...


class RagService(ABC):
    """Parsing, ingestion and retrieval over the internal regulation corpus."""

    name: str = ""

    # Optional capabilities. Providers whose service does not expose the backing
    # endpoint set these False; callers must use ``getattr(service, flag, True)``
    # because test doubles are duck-typed and may not define them.
    supports_parse = True  # external document parsing returns plain text
    supports_document_listing = True  # documents() / stats()
    supports_document_chunks = True  # fetch_chunks()

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether the provider is configured and reachable enough to try."""

    @abstractmethod
    def parse(self, path: Path, doc_id: str) -> ParsedDoc:
        """Parse one external document into plain text."""

    @abstractmethod
    def ingest(
        self, path: Path, name: str, doc_id: str = "", *, labels: list[str] | None = None
    ) -> DocInfo:
        """Parse, chunk and index one internal document.

        ``labels`` names the corpus the document belongs to; providers that
        isolate corpora some other way (chillrag: one folder) ignore it.
        """

    @abstractmethod
    def search(self, query: str, k: int) -> list[Hit]:
        """Hybrid retrieval over the internal corpus."""

    @abstractmethod
    def fetch_chunks(self, doc_id: str) -> list[Chunk]:
        """Every chunk of one internal document, in reading order."""

    @abstractmethod
    def documents(self) -> list[DocInfo]:
        """Documents currently in the internal corpus."""

    @abstractmethod
    def stats(self) -> CorpusStats:
        """Corpus counters and the version used for analysis snapshots."""

    def delete(self, doc_id: str) -> bool:
        """Remove one document; ``False`` when the provider cannot delete."""
        return False
