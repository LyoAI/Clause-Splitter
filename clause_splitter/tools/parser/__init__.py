"""Offline ingestion stage interfaces: parse → chunk.

- ``blocks.Block`` — the parse-stage output / chunk-stage input contract.
- ``mineru_parse.parse_docs`` — documents (pdf / image / docx / pptx / xlsx)
  → blocks (local MinerU).
- ``chunker.chunk_doc`` — blocks → retrieval units (hierarchy-aware).
- ``chunker.doc_sections`` — blocks → table of contents (``Section`` records
  for every boundary heading; backs the ``get_toc`` agent tool).
- ``chunker.estimate_tokens`` — tokenizer-free fallback estimator; chunk
  budgets default to the embedding model's tokenizer (``TokenizerCounter``).

Downstream stages (embedding, searchers) consume ``DocumentStore``, never these
directly.
"""

from .blocks import Block
from .chunker import chunk_doc, doc_sections, estimate_tokens
from .mineru_parse import parse_docs

__all__ = ["Block", "chunk_doc", "doc_sections", "parse_docs", "estimate_tokens"]
