"""External document parsing (remote RAG service first, local text fallback)."""

from .blocks import Block, blocks_from_markdown, blocks_from_remote, blocks_from_text
from .normalize import normalize_text
from .router import ParsedDocument, parse_document

__all__ = [
    "Block",
    "ParsedDocument",
    "blocks_from_markdown",
    "blocks_from_remote",
    "blocks_from_text",
    "normalize_text",
    "parse_document",
]
