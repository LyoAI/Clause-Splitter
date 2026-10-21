"""Parse-stage dispatch over a folder's mixed source documents.

A folder holds whatever the caller put in it: PDFs and Office files that MinerU
reads, and markdown that arrives already converted. Both front ends project onto
the same ``Block`` schema, so the chunker and every stage below it are unaware
of which path a document took — this module is the only place that knows, and it
exists so the corpus layer does not have to.

``doc_id`` is the filename either way, which is what ``RAGTool.file_info`` keys
on and what ``get_segments`` reads back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from chillrag.utils import get_logger

from .blocks import Block
from .mineru_parse import SUPPORTED_SUFFIXES, parse_docs
from .parse_markdown import parse_markdown_file

logger = get_logger()

# Markdown needs no MinerU run, so it is parsed in-process. ``.markdown`` is
# accepted alongside ``.md`` because it costs nothing to recognize.
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

# Every suffix a corpus folder may contain. This is what the folder scan and its
# error message report, so a document is never silently skipped.
SOURCE_SUFFIXES = SUPPORTED_SUFFIXES | MARKDOWN_SUFFIXES


def parse_sources(
    doc_paths: Iterable[Path], out_dir: Path | None = None
) -> dict[str, list[Block]]:
    """Parse a batch of documents, routing each to the front end it needs.

    Args:
        doc_paths: Documents to parse (markdown and MinerU-readable formats may
            be mixed in one batch).
        out_dir: MinerU output root; ignored for markdown, which has no
            intermediate files.

    Returns:
        ``{doc_id: [Block, ...]}`` in reading order, ready for ``chunk_doc`` —
        the same shape and ``doc_id`` convention as ``parse_docs``.

    Raises:
        ValueError: a document is neither markdown nor MinerU-readable.
        FileNotFoundError: a markdown document does not exist.
    """
    paths = [Path(p) for p in doc_paths]
    markdown = [p for p in paths if p.suffix.lower() in MARKDOWN_SUFFIXES]
    others = [p for p in paths if p.suffix.lower() not in MARKDOWN_SUFFIXES]
    # MinerU rejects a batch containing a suffix it does not know, so markdown
    # is split off before this call rather than filtered inside it.
    parsed = parse_docs(others, out_dir=out_dir) if others else {}
    for path in markdown:
        if not path.is_file():
            logger.error("no such document: %s", path)
            raise FileNotFoundError(f"no such document: {path}")
        blocks = parse_markdown_file(path)
        parsed[path.name] = blocks
        logger.info("%s: %d blocks (markdown)", path.name, len(blocks))
    return parsed
