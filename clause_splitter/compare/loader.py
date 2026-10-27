"""Per-article clauses from a split-stage chunk stream."""

from __future__ import annotations

from clause_splitter.config import CHUNK_LEVEL, CHUNK_MIN_TOKENS, CHUNK_SIZE
from clause_splitter.rag.types import Chunk
from clause_splitter.split.chunker import chunk_doc

from .episode import ExternalClause


def _external_clause_from_chunk(doc_id: str, chunk: Chunk) -> ExternalClause:
    """One chunk = one clause; the clause number is the section path's leaf."""
    section = str(chunk.meta.get("section", "") or "").strip()
    leaf = section.split(" > ")[-1].strip() if " > " in section else section
    return ExternalClause(
        document_name=doc_id,
        clause_number=leaf or "全文",
        section=section or None,
        full_text=chunk.text,
    )


def clauses_from_blocks(doc_id: str, blocks: list) -> list[ExternalClause]:
    """Heading-classified blocks → per-article clauses (same contract as the
    internal library: level/size/min_tokens from config)."""
    chunks = chunk_doc(
        doc_id, blocks, level=CHUNK_LEVEL, chunk_size=CHUNK_SIZE, min_tokens=CHUNK_MIN_TOKENS
    )
    return [
        c for c in (_external_clause_from_chunk(doc_id, chunk) for chunk in chunks)
        if c.full_text.strip()
    ]
