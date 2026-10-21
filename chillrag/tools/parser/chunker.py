"""Chunk-stage: blocks → retrieval units (Chunk), hierarchy-aware.

Splits a document's blocks into chunks by heading hierarchy: a heading block
with ``level <= CHUNK_LEVEL`` opens a new chunk (and updates the section path);
deeper sub-headings (``level > CHUNK_LEVEL``) stay inside the current chunk so
the granularity never gets too fine. A document with no headings (or only
deeper-than-``level`` headings) falls back to ``meta.section = doc_id`` so the
chunks still carry a retrieval anchor. Tables stay embedded in their section —
never split, and an oversized table travels with the lead-in paragraph that
precedes it. Page furniture (header / footer / page_number) never enters chunk
text; it accumulates into chunk ``meta`` instead, so repeated page banners
don't collapse BM25 IDF or pollute embeddings.

Chunk ``meta`` keys: ``page`` / ``pages`` (1-based span), ``section`` (heading
path, e.g. "Chapter 2 > Methods"; falls back to the doc id for
heading-less documents), ``table_ids`` (reading-order table numbers, e.g.
["T3", "T4"]), ``content_types`` / ``native_types`` (the form of content in the
chunk — see below), and optional ``header`` / ``footer``.

Content typing: a chunk records the **native** MinerU types of the blocks that
formed it (``native_types``) plus their projection onto the coarse vocabulary a
retrieval caller filters on (``content_types``). Both are *sets* — a chunk can
mix a table into its surrounding prose — so a filter matches on intersection.
The heading that *opens* a chunk is excluded: it is the chunk's structural
anchor (already in ``meta.section``), not body content, so including it would
make every chunk match a ``title`` filter and mean nothing.

``doc_sections`` produces the document's table of contents (the ``get_toc``
tool's source): it prefers the printed 目錄 (the ``index`` block), and falls
back to boundary headings — the same predicate that opens chunks — when no
parseable 目錄 exists.

Chunk budgets are measured in the embedding model's own tokenizer
(``TokenizerCounter`` on ``EMBED_MODEL``), so ``chunk_size`` means the same thing
to chunking and to the embedding context window; the tokenizer-free heuristic is
the fallback when the tokenizer can't be loaded.
"""

from __future__ import annotations

import re
from typing import Iterable

from chillrag.config import CHUNK_LEVEL, CHUNK_MIN_TOKENS, CHUNK_SIZE, EMBED_MODEL
from chillrag.utils import get_logger

from ..searcher.store import Chunk, Section
from .blocks import META_TYPES, Block, coarse_types

logger = get_logger()

_HEADER_KINDS = frozenset({"header", "page_header"})
_FOOTER_KINDS = frozenset({"footer", "page_footer"})


def _opens_section(block: Block, level: int) -> bool:
    """True when ``block`` is a boundary heading: a title at level ≤ ``level``
    with non-empty text. The single source of truth for both chunk boundaries
    and the TOC (``doc_sections``), so the two always agree."""
    return block.level is not None and block.level <= level and bool(block.text.strip())


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 CJK char ≈ 1 token, ~4 ASCII chars ≈ 1 token.

    Deliberately tokenizer-free so chunking is deterministic and cheap; only the
    ordering of split decisions matters, not exact token counts.
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk + (len(text) - cjk + 3) // 4


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x2E80 <= o <= 0x9FFF  # CJK radicals, unified ideographs
        or 0xF900 <= o <= 0xFAFF  # CJK compatibility ideographs
        or 0xFF00 <= o <= 0xFFEF  # fullwidth forms
        or 0x3000 <= o <= 0x303F  # CJK punctuation
    )


# ---------------------------------------------------------------------------
# Token budgets: the embedding model's tokenizer, heuristic as the fallback.
# ``chunk_size`` should mean the same thing to chunking and to the embedding
# model's context window, so we count with the very tokenizer that later
# vectorizes the chunks.

_UNRESOLVED = object()


class TokenizerCounter:
    """Callable token count backed by the embedding model's tokenizer.

    Resolves lazily on first call, then reuses it. Falls back to the
    tokenizer-free heuristic when the tokenizer can't be loaded (missing
    model, no network).
    """

    def __init__(self, model: str = EMBED_MODEL) -> None:
        self._model = model
        self._tokenizer = _UNRESOLVED

    def __call__(self, text: str) -> int:
        if not text:
            return 0
        tok = self._resolve()
        if tok is None:
            return estimate_tokens(text)
        return len(tok.encode(text))

    def _resolve(self):
        if self._tokenizer is _UNRESOLVED:
            self._tokenizer = _load_tokenizer(self._model)
            if self._tokenizer is not None:
                logger.info("counting chunk tokens with %s", self._model)
        return self._tokenizer


def _load_tokenizer(model: str):
    """AutoTokenizer for ``model`` (a local path or HF/modelscope id), or ``None``.

    ``None`` is not an error: chunk budgets then fall back to the character
    heuristic below, which is coarser but needs no model on disk.
    """
    try:
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(model)
    except Exception as e:
        logger.warning("no tokenizer for %s (%s); estimating chunk tokens instead", model, e)
        return None


# Default estimator for chunk budgets: the embedding model's tokenizer.
DEFAULT_TOKEN_COUNT = TokenizerCounter()


# Split point right after a sentence-ending mark (kept attached). Also breaks on
# line breaks so oversized list/code blocks split at item boundaries.
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；!?;\n])")


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences, each keeping its ending punctuation."""
    parts = [p for p in _SENTENCE_SPLIT.split(text) if p]
    return parts or [text]


def _hard_cut(text: str, chunk_size: int) -> list[str]:
    """Last-resort cut of a single unpunctuated run into ``chunk_size`` pieces.

    Only reached when a block has no sentence boundary at all within the budget
    (rare in prose). Uses the tokenizer-free heuristic per character so the walk
    stays linear and cheap — this emergency path only needs a reasonable cut.
    """
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
    """Group a document's blocks into retrieval units (chunk stage).

    Args:
        doc_id: The document identity; every ``chunk_id`` is
            ``f"{doc_id}#{order}"``.
        blocks: Parse-stage blocks in reading order (``mineru_parse`` output).
        level: Heading levels ≤ this open a new chunk; deeper sub-headings stay
            inside the current chunk.
        chunk_size: Soft token budget per chunk. Oversized sections are split
            at block boundaries; tables are never cut in half (a table over the
            budget becomes its own chunk, otherwise it carries its lead-in into
            a fresh chunk).
        min_tokens: Chunks smaller than this (tokens) merge into the previous
            chunk when the section path matches. 0 = strict section boundaries.
        estimate_tokens: Token estimator. Defaults to ``DEFAULT_TOKEN_COUNT``
            (the embedding model's tokenizer, heuristic fallback); injectable
            for deterministic tests.
    """
    estimate_tokens = estimate_tokens or DEFAULT_TOKEN_COUNT
    builder = _ChunkBuilder(
        doc_id, level=level, chunk_size=chunk_size, estimate_tokens=estimate_tokens
    )
    for block in blocks:
        builder.add(block)
    builder.finish()
    return merge_small_chunks(builder.chunks, min_tokens, estimate_tokens)


def doc_sections(
    doc_id: str,
    blocks: Iterable[Block],
    *,
    level: int = CHUNK_LEVEL,
) -> list[Section]:
    """Table of contents of ``blocks`` (the ``get_toc`` tool's source).

    Prefers the document's printed 目錄 — the first ``index`` block, whose lines
    are ``- <title> <printed page>``. The printed→physical page offset is derived
    by matching the first 目錄 title against a heading block (annual-report
    front matter is a constant offset) and applied to every entry. Falls back to
    boundary headings (title level ≤ ``level``, the same predicate that opens
    chunks) when no parseable 目錄 exists — e.g. an appendix index whose lines
    aren't "title page". ``section_id`` is ``f"{doc_id}#s{i}"`` with ``i`` the
    entry's 0-based position.
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
            continue  # not a page-numbered TOC (e.g. an address appendix)
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
    """Parse ``- <title> <page>`` lines into ``(title, page)`` pairs.

    Only pages in ``[1, max_page]`` count: the 目錄's printed pages are at most
    the last physical page, while appendix "index" lines end in postal codes
    that blow past it. Non-matching lines are dropped.
    """
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


# ---------------------------------------------------------------------------


class _ChunkBuilder:
    """Accumulates one document's chunks from its block stream."""

    def __init__(self, doc_id, *, level, chunk_size, estimate_tokens):
        self.doc_id = doc_id
        self.level = level
        self.chunk_size = chunk_size
        self.estimate_tokens = estimate_tokens
        self.chunks: list[Chunk] = []
        # Current chunk: (text, page, native type) per block, running token count.
        # ``None`` marks the structural heading that opened the chunk, which
        # contributes text but is not counted as body content type.
        self.buffer: list[tuple[str, int, str | None]] = []
        self.buffer_tokens = 0
        # Native types of the blocks in the buffer, in reading order (dupes kept
        # until ``_flush`` dedupes) — projected onto ``content_types`` there.
        self.buffer_types: list[str] = []
        self.start_page: int | None = None
        self.end_page: int | None = None
        # True when the buffer holds only a boundary heading (no body yet).
        self.bare_heading = False
        # Heading path of the section currently being filled.
        self.stack: list[str] = []
        # Reading-order table counter; ids shared across the whole document.
        self.table_counter = 0
        self.current_tables: list[str] = []
        # Last non-empty page banner seen; lands in chunk meta, never in text.
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

    # -------------------------------------------------------------- internals

    def _update_stack(self, block: Block) -> None:
        while len(self.stack) >= block.level:
            self.stack.pop()
        self.stack.append(block.text.strip() or block.text)

    def _start(self, text: str, page: int, native: str | None = None) -> None:
        """Begin the current chunk with ``text`` (page furniture already seen).

        ``native`` is the block's MinerU type, or ``None`` when ``text`` is the
        structural heading that opens the chunk.
        """
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
            # A single prose block over the budget: cut at sentence boundaries
            # so no chunk blows the budget — and following blocks don't get
            # fragmented. Parts pack into the current chunk until it is full,
            # then a fresh chunk starts.
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
        # A single table over the budget is atomic: it becomes its own chunk
        # (the section title still reaches it through ``meta.section``).
        if tok > self.chunk_size:
            self._flush()
            self._start(block.text, block.page, block.type)
            self._record_table()
            self._flush()
            return
        if self.buffer and self.buffer_tokens + tok > self.chunk_size:
            # Never split a table: the cut lands before its lead-in so the
            # table travels with the paragraph that introduces it.
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
        """Cut an oversized non-table block into budget-fitting sentence groups.

        Sentences are greedily packed to fill ``chunk_size``; a single sentence
        that still overflows is hard-cut as a last resort. Tables never reach
        here (they are atomic in ``_append_content``).
        """
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
        # page_number / page_aside_text / page_footnote: nothing to record.

    def _flush(self) -> None:
        """Emit the current buffer as a chunk, unless it is heading-only."""
        if not self.buffer:
            return
        if self.bare_heading and len(self.buffer) == 1:
            # A lone heading with no body retrieves nothing; its title already
            # lives in the section path meta of the chunks that follow.
            self._clear_buffer()
            return
        text = "\n\n".join(t for t, _, _ in self.buffer)
        meta = {
            "page": self.start_page,
            "pages": _page_range(self.start_page, self.end_page),
            # Heading path, or the doc id for heading-less documents so the
            # chunk still carries a retrieval anchor.
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
        """Reset the per-chunk accumulation state (buffer, types, page span)."""
        self.buffer = []
        self.buffer_types = []
        self.buffer_tokens = 0
        self.start_page = self.end_page = None
        self.current_tables = []
        self.cur_header = self.cur_footer = ""
        self.bare_heading = False


# ---------------------------------------------------------------------------


def merge_small_chunks(
    chunks: list[Chunk], min_tokens: int, estimate_tokens=None
) -> list[Chunk]:
    """Fold chunks below ``min_tokens`` into the previous same-section chunk.

    Fights over-fragmentation when a section spills: tiny trailing chunks merge
    back so their retrieval budget is not wasted. A chunk that opens a new
    section is never folded (section boundaries stay honest), and a chunk that
    already swallowed others stays eligible.
    """
    estimate_tokens = estimate_tokens or DEFAULT_TOKEN_COUNT
    if min_tokens <= 0 or len(chunks) < 2:
        return chunks
    merged: list[Chunk] = []
    for chunk in chunks:
        if merged and estimate_tokens(chunk.text) < min_tokens:
            prev = merged[-1]
            if prev.meta.get("section") == chunk.meta.get("section"):
                merged[-1] = _absorb(prev, chunk, estimate_tokens)
                continue
        merged.append(chunk)
    return merged


def _absorb(prev: Chunk, chunk: Chunk, estimate_tokens) -> Chunk:
    """Return ``prev`` extended with ``chunk``'s text and provenance."""
    prev.text = prev.text + "\n\n" + chunk.text
    prev_end = int(str(prev.meta.get("pages", "")).split("-")[-1] or 0) or prev.meta.get("page", 0)
    chunk_end = int(str(chunk.meta.get("pages", "")).split("-")[-1] or 0) or chunk.meta.get("page", 0)
    prev.meta["pages"] = _page_range(prev.meta.get("page"), max(prev_end, chunk_end))
    tables = list(prev.meta.get("table_ids") or []) + list(chunk.meta.get("table_ids") or [])
    if tables:
        prev.meta["table_ids"] = tables
    # Folded text brings its content types along, so a filter still sees the
    # table that just moved into this chunk.
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
