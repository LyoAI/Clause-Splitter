"""Blocks → per-article chunks, hierarchy-aware.

A heading block with ``level <= CHUNK_LEVEL`` opens a new chunk (and updates
the section path); deeper sub-headings stay inside the current chunk. Tables
are atomic; page furniture accumulates into chunk ``meta`` instead of text.

Chunk ``meta`` keys: ``page`` / ``pages``, ``section`` (heading path, e.g.
"第一章 总则 > 第二条"; falls back to the doc id), ``table_ids``,
``content_types`` / ``native_types``, optional ``header`` / ``footer``.

Token budgets use a dependency-free estimate (1 CJK char ≈ 1 token, ~4 ASCII
chars ≈ 1 token) — deterministic and sufficient for split ordering.
"""

from __future__ import annotations

import re
from typing import Iterable

from clause_splitter.config import CHUNK_LEVEL, CHUNK_MIN_TOKENS, CHUNK_SIZE
from clause_splitter.parsing.blocks import META_TYPES, Block, coarse_types
from clause_splitter.rag.types import Chunk, Section
from clause_splitter.utils import get_logger

logger = get_logger()

_HEADER_KINDS = frozenset({"header", "page_header"})
_FOOTER_KINDS = frozenset({"footer", "page_footer"})


def _opens_section(block: Block, level: int) -> bool:
    """True when ``block`` is a boundary heading (title level ≤ ``level``)."""
    return block.level is not None and block.level <= level and bool(block.text.strip())


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 CJK char ≈ 1 token, ~4 ASCII chars ≈ 1 token."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk + (len(text) - cjk + 3) // 4


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x2E80 <= o <= 0x9FFF
        or 0xF900 <= o <= 0xFAFF
        or 0xFF00 <= o <= 0xFFEF
        or 0x3000 <= o <= 0x303F
    )


# Split point right after a sentence-ending mark (kept attached). Also breaks on
# line breaks so oversized list/code blocks split at item boundaries.
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；!?;\n])")


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences, each keeping its ending punctuation."""
    parts = [p for p in _SENTENCE_SPLIT.split(text) if p]
    return parts or [text]


def _hard_cut(text: str, chunk_size: int) -> list[str]:
    """Last-resort cut of a single unpunctuated run into ``chunk_size`` pieces."""
    parts: list[str] = []
    cur, cur_tok = "", 0
    for ch in text:
        tok = estimate_tokens(ch)
        if cur and cur_tok + tok > chunk_size:
            parts.append(cur)
            cur, cur_tok = "", 0
        cur += ch
        cur_tok += tok
    if cur:
        parts.append(cur)
    return parts


def chunk_doc(
    doc_id: str,
    blocks: Iterable[Block],
    *,
    level: int = CHUNK_LEVEL,
    chunk_size: int = CHUNK_SIZE,
    min_tokens: int = CHUNK_MIN_TOKENS,
    estimate_tokens=None,
) -> list[Chunk]:
    """Group a document's blocks into retrieval units.

    ``chunk_id`` is ``f"{doc_id}#{order}"``; the order is the chunk's 0-based
    position, which ``get_context`` windows rely on.
    """
    estimate_tokens = estimate_tokens or estimate_tokens_default
    builder = _ChunkBuilder(
        doc_id, level=level, chunk_size=chunk_size, estimate_tokens=estimate_tokens
    )
    for block in blocks:
        builder.add(block)
    builder.finish()
    return merge_small_chunks(builder.chunks, min_tokens, estimate_tokens)


def estimate_tokens_default(text: str) -> int:
    return estimate_tokens(text)


def doc_sections(
    doc_id: str,
    blocks: Iterable[Block],
    *,
    level: int = CHUNK_LEVEL,
) -> list[Section]:
    """Table of contents of ``blocks``.

    Prefers a printed 目錄 (the first ``index`` block) with a derived
    printed→physical page offset; falls back to boundary headings.
    """
    blocks = list(blocks)
    toc = _toc_from_index(doc_id, blocks)
    if toc is not None:
        return toc
    return [
        Section(
            section_id=f"{doc_id}#s{i}",
            title=block.text.strip() or block.text,
            level=block.level,  # _opens_section guarantees not None
            page=block.page,
        )
        for i, block in enumerate(b for b in blocks if _opens_section(b, level))
    ]


def _toc_from_index(doc_id: str, blocks: list[Block]) -> list[Section] | None:
    """Build the TOC from the front-matter 目錄 (``index`` block), if present."""
    max_page = max((b.page for b in blocks), default=0)
    for block in blocks:
        if block.type != "index":
            continue
        items = _parse_index_items(block.text, max_page)
        if not items:
            continue
        offset = _page_offset(blocks, items[0][0], items[0][1])
        return [
            Section(
                section_id=f"{doc_id}#s{i}",
                title=title,
                level=1,
                page=printed + offset,
            )
            for i, (title, printed) in enumerate(items)
        ]
    return None


def _parse_index_items(text: str, max_page: int) -> list[tuple[str, int]]:
    """Parse ``- <title> <page>`` lines into ``(title, page)`` pairs."""
    items: list[tuple[str, int]] = []
    for line in text.splitlines():
        line = line.strip().lstrip("*- ")
        title, _, page = line.rpartition(" ")
        if page.isdigit() and 1 <= int(page) <= max_page:
            items.append((title, int(page)))
    return items


def _page_offset(blocks: list[Block], title: str, printed: int) -> int:
    """Physical page of ``title``'s first heading minus its printed page."""
    for b in blocks:
        if b.type == "title" and b.text.strip() == title:
            return b.page - printed
    return 0


class _ChunkBuilder:
    """Accumulates one document's chunks from its block stream."""

    def __init__(self, doc_id, *, level, chunk_size, estimate_tokens):
        self.doc_id = doc_id
        self.level = level
        self.chunk_size = chunk_size
        self.estimate_tokens = estimate_tokens
        self.chunks: list[Chunk] = []
        self.buffer: list[tuple[str, int, str | None]] = []
        self.buffer_tokens = 0
        self.buffer_types: list[str] = []
        self.start_page: int | None = None
        self.end_page: int | None = None
        self.bare_heading = False
        self.stack: list[str] = []
        self.table_counter = 0
        self.current_tables: list[str] = []
        self.cur_header = ""
        self.cur_footer = ""
        self.order = 0

    def add(self, block: Block) -> None:
        if block.type in META_TYPES:
            self._absorb_furniture(block)
            return
        if _opens_section(block, self.level):
            self._flush()
            self._update_stack(block)
            self._start(block.text, block.page)
            self.bare_heading = True
            return
        self._append_content(block)

    def finish(self) -> None:
        self._flush()

    def _update_stack(self, block: Block) -> None:
        while len(self.stack) >= block.level:
            self.stack.pop()
        self.stack.append(block.text.strip() or block.text)

    def _start(self, text: str, page: int, native: str | None = None) -> None:
        """Begin the current chunk with ``text`` (page furniture already seen)."""
        self.buffer = [(text, page, native)]
        self.buffer_types = [native] if native else []
        self.buffer_tokens = self.estimate_tokens(text)
        self.start_page = self.end_page = page
        self.current_tables = []

    def _append_content(self, block: Block) -> None:
        tok = self.estimate_tokens(block.text)
        if block.type == "table":
            self._append_table(block, tok)
            return
        if tok > self.chunk_size:
            for part in self._budget_parts(block):
                if self.buffer and (
                    self.buffer_tokens + self.estimate_tokens(part)
                    > self.chunk_size
                ):
                    self._flush()
                self._append(part, block.page, block.type)
            return
        if self.buffer and self.buffer_tokens + tok > self.chunk_size:
            self._flush()
            self._append(block.text, block.page, block.type)
        else:
            self._append(block.text, block.page, block.type)

    def _append_table(self, block: Block, tok: int) -> None:
        # Never split a table; an oversized table becomes its own chunk.
        if tok > self.chunk_size:
            self._flush()
            self._start(block.text, block.page, block.type)
            self._record_table()
            self._flush()
            return
        if self.buffer and self.buffer_tokens + tok > self.chunk_size:
            lead = self._pop_last()
            self._flush()
            if lead is not None:
                self._start(*lead)
            self._append(block.text, block.page, block.type)
            self._record_table()
        else:
            self._append(block.text, block.page, block.type)
            self._record_table()

    def _append(self, text: str, page: int, native: str | None = None) -> None:
        if not self.buffer:
            self._start(text, page, native)
        else:
            self.buffer.append((text, page, native))
            if native:
                self.buffer_types.append(native)
            self.buffer_tokens += self.estimate_tokens(text)
            self.end_page = page
        self.bare_heading = False

    def _budget_parts(self, block: Block) -> list[str]:
        """Cut an oversized non-table block into budget-fitting sentence groups."""
        text = block.text
        if self.estimate_tokens(text) <= self.chunk_size:
            return [text]
        out: list[str] = []
        pending = ""
        for sentence in _split_sentences(text):
            if pending and (
                self.estimate_tokens(pending) + self.estimate_tokens(sentence)
                > self.chunk_size
            ):
                out.append(pending)
                pending = sentence
            else:
                pending += sentence
            if pending and self.estimate_tokens(pending) > self.chunk_size:
                out.extend(_hard_cut(pending, self.chunk_size))
                pending = ""
        if pending:
            out.append(pending)
        return out

    def _pop_last(self) -> tuple[str, int, str | None] | None:
        if not self.buffer:
            return None
        item = self.buffer.pop()
        if item[2]:
            self.buffer_types.remove(item[2])
        self.buffer_tokens -= self.estimate_tokens(item[0])
        if self.buffer:
            self.end_page = self.buffer[-1][1]
        else:
            self.start_page = self.end_page = None
        return item

    def _record_table(self) -> None:
        self.table_counter += 1
        self.current_tables.append(f"T{self.table_counter}")

    def _absorb_furniture(self, block: Block) -> None:
        text = block.text.strip()
        if not text:
            return
        if block.type in _HEADER_KINDS:
            self.cur_header = text
        elif block.type in _FOOTER_KINDS:
            self.cur_footer = text

    def _flush(self) -> None:
        """Emit the current buffer as a chunk, unless it is heading-only."""
        if not self.buffer:
            return
        if self.bare_heading and len(self.buffer) == 1:
            self._clear_buffer()
            return
        text = "\n\n".join(t for t, _, _ in self.buffer)
        meta = {
            "page": self.start_page,
            "pages": _page_range(self.start_page, self.end_page),
            "section": " > ".join(self.stack) or self.doc_id,
        }
        native_types = list(dict.fromkeys(self.buffer_types))
        if native_types:
            meta["native_types"] = native_types
            meta["content_types"] = coarse_types(native_types)
        if self.current_tables:
            meta["table_ids"] = list(self.current_tables)
        if self.cur_header:
            meta["header"] = self.cur_header
        if self.cur_footer:
            meta["footer"] = self.cur_footer
        self.chunks.append(
            Chunk(
                chunk_id=f"{self.doc_id}#{self.order}",
                doc_id=self.doc_id,
                order=self.order,
                text=text,
                meta=meta,
            )
        )
        self.order += 1
        self._clear_buffer()

    def _clear_buffer(self) -> None:
        """Reset the per-chunk accumulation state."""
        self.buffer = []
        self.buffer_types = []
        self.buffer_tokens = 0
        self.start_page = self.end_page = None
        self.current_tables = []
        self.cur_header = self.cur_footer = ""
        self.bare_heading = False


def merge_small_chunks(
    chunks: list[Chunk], min_tokens: int, estimate_tokens=None
) -> list[Chunk]:
    """Fold chunks below ``min_tokens`` into the previous same-section chunk."""
    estimate_tokens = estimate_tokens or estimate_tokens_default
    if min_tokens <= 0 or len(chunks) < 2:
        return chunks
    merged: list[Chunk] = []
    for chunk in chunks:
        if merged and estimate_tokens(chunk.text) < min_tokens:
            prev = merged[-1]
            if prev.meta.get("section") == chunk.meta.get("section"):
                merged[-1] = _absorb(prev, chunk)
                continue
        merged.append(chunk)
    return merged


def _absorb(prev: Chunk, chunk: Chunk) -> Chunk:
    """Return ``prev`` extended with ``chunk``'s text and provenance."""
    prev.text = prev.text + "\n\n" + chunk.text
    prev_end = int(str(prev.meta.get("pages", "")).split("-")[-1] or 0) or prev.meta.get("page", 0)
    chunk_end = int(str(chunk.meta.get("pages", "")).split("-")[-1] or 0) or chunk.meta.get("page", 0)
    prev.meta["pages"] = _page_range(prev.meta.get("page"), max(prev_end, chunk_end))
    tables = list(prev.meta.get("table_ids") or []) + list(chunk.meta.get("table_ids") or [])
    if tables:
        prev.meta["table_ids"] = tables
    native = list(prev.meta.get("native_types") or []) + list(chunk.meta.get("native_types") or [])
    if native:
        native = list(dict.fromkeys(native))
        prev.meta["native_types"] = native
        prev.meta["content_types"] = coarse_types(native)
    if not prev.meta.get("header") and chunk.meta.get("header"):
        prev.meta["header"] = chunk.meta["header"]
    if not prev.meta.get("footer") and chunk.meta.get("footer"):
        prev.meta["footer"] = chunk.meta["footer"]
    return prev


def _page_range(start: int | None, end: int | None) -> str:
    if start is None:
        return ""
    if end is None or end == start:
        return str(start)
    return f"{start}-{end}"
