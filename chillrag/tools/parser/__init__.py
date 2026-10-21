"""Offline ingestion stage interfaces: parse → chunk.

- ``blocks.Block`` — the parse-stage output / chunk-stage input contract.
- ``blocks.CONTENT_TYPES`` / ``blocks.coarse_type`` — the content-form labels a
  chunk is tagged with (``excel`` / ``text`` / ``title`` / ``image``), which
  back ``search_by_question``'s ``recall_content_types`` filter.
- ``mineru_parse.parse_docs`` — documents (pdf / image / docx / pptx / xlsx)
  → blocks (local MinerU).
- ``parse_markdown.parse_markdown`` — markdown text → blocks, for corpora that
  arrive already converted (no MinerU run, no document files).
- ``sources.parse_sources`` — a folder's whole batch, each document routed to the
  front end its suffix needs; ``sources.SOURCE_SUFFIXES`` is every suffix a
  folder may contain.
- ``chunker.chunk_doc`` — blocks → retrieval units (hierarchy-aware).
- ``chunker.doc_sections`` — blocks → table of contents (``Section`` records
  for every boundary heading; backs the ``get_toc`` agent tool).
- ``chunker.estimate_tokens`` — tokenizer-free fallback estimator; chunk
  budgets default to the embedding model's tokenizer (``TokenizerCounter``).

Downstream stages (embedding, searchers) consume ``DocumentStore``, never these
directly.
"""

from .blocks import CONTENT_TYPES, Block, coarse_type, coarse_types
from .chunker import chunk_doc, doc_sections, estimate_tokens
from .mineru_parse import parse_docs
from .parse_markdown import parse_markdown, parse_markdown_file
from .sources import MARKDOWN_SUFFIXES, SOURCE_SUFFIXES, parse_sources

__all__ = [
    "Block",
    "CONTENT_TYPES",
    "MARKDOWN_SUFFIXES",
    "SOURCE_SUFFIXES",
    "chunk_doc",
    "coarse_type",
    "coarse_types",
    "doc_sections",
    "estimate_tokens",
    "parse_docs",
    "parse_markdown",
    "parse_markdown_file",
    "parse_sources",
]
