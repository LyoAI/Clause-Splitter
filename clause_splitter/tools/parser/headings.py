"""Regulation-heading fallback for heading-less documents.

MinerU's layout detection only emits ``title`` blocks when a document uses real
heading styles (Word heading styles, or PDF font-size heuristics). Hand-written
regulations often bold the article markers instead (``**第一条**　…``) and keep
every paragraph in the ``Normal`` style, so MinerU produces *no* title blocks
and the whole regulation collapses into a single chunk.

``infer_regulation_headings`` is an additive pre-pass for that case: when a
document has no ``title`` blocks at all, it re-interprets paragraph blocks that
open with a regulation heading (第X章 / 第X节 / 第X条) as headings, so the chunker
can group by chapter / section again:

- 章 / 节 headings open chunks (level 1 / 2 — at or below ``CHUNK_LEVEL``).
- 条 headings stay *inside* the enclosing chapter chunk (level
  ``CHUNK_LEVEL + 1``) when the document has chapters, so several articles
  aggregate into one chapter-level retrieval unit instead of one chunk per
  clause. A chapter-less regulation falls back to one chunk per 条.
"""

from __future__ import annotations

import re

from clause_splitter.config import CHUNK_LEVEL
from .blocks import Block

# Opening regulation heading, e.g. ``第一章`` / ``第一节`` / ``第一条``.
# ``\s*`` around the numeral tolerates sloppy hand-typing ("第 一 条").
_HEADING_RE = re.compile(r"^\s*\**\s*(第\s*[一二三四五六七八九十百零两]+\s*[章节条款])")
# Chapters / sections only — used to decide whether 条 should open chunks.
_CHAPTER_RE = re.compile(r"^\s*\**\s*第\s*[一二三四五六七八九十百零两]+\s*[章节]")

_CHAPTER_LEVELS = {"章": 1, "节": 2}
# 条 sits above CHUNK_LEVEL so it never opens a chunk by itself when chapters
# exist; the 条 label stays in the chunk text as a retrieval anchor.
_ARTICLE_LEVEL = CHUNK_LEVEL + 1

# A short, punctuation-free opening line that is not a regulation heading —
# the document title (e.g. "网络数据安全管理条例"). Re-classified as a level-1
# heading so the chunker drops it as a bare heading when the first real chapter
# arrives, instead of leaving a noisy one-line retrieval unit.
_DOC_TITLE_RE = re.compile(r"^[^。！？；;:：,，]{1,40}$")


def _looks_like_doc_title(text: str) -> bool:
    return bool(text) and _DOC_TITLE_RE.match(text) and not _HEADING_RE.match(text)


def infer_regulation_headings(blocks: list[Block]) -> list[Block]:
    """Re-classify regulation headings in a document that has no title blocks.

    Returns ``blocks`` unchanged when MinerU already detected headings, or when
    nothing matches a regulation heading pattern. Otherwise returns a new block
    list where each heading paragraph is split into a ``title`` block (computed
    level) plus its body.
    """
    if any(b.type == "title" for b in blocks):
        return blocks  # MinerU found real headings — nothing to do.
    blocks = list(blocks)
    if not any(_HEADING_RE.match(b.text.strip()) for b in blocks):
        return blocks  # not a regulation-shaped document.
    has_chapter = any(_CHAPTER_RE.match(b.text.strip()) for b in blocks)

    out: list[Block] = []
    order = 0
    for i, b in enumerate(blocks):
        text = b.text.strip()
        # The document title (first line, short, punctuation-free, not a
        # heading) becomes a level-1 heading: the chunker drops it as a bare
        # heading the moment the first real chapter opens, so the library has
        # no noisy one-line retrieval unit for the title.
        if i == 0 and b.type == "paragraph" and _looks_like_doc_title(text):
            out.append(Block(b.block_id, "title", text, b.page, order, 1))
            order += 1
            continue
        m = _HEADING_RE.match(text) if b.type == "paragraph" else None
        if m:
            marker = m.group(1)
            kind = marker[-1]
            if kind in _CHAPTER_LEVELS:
                # 章 / 节: the whole line is the heading (e.g. 第一章　总则).
                title = text.strip("* ").strip()
                out.append(Block(b.block_id, "title", title, b.page, order, _CHAPTER_LEVELS[kind]))
                order += 1
            else:
                # 条: split into heading + body so the article label anchors the
                # chunk text while the body feeds retrieval. With chapters, 条 sits
                # at CHUNK_LEVEL + 1 (stays intra-chunk); without chapters it is the
                # document's own top structure, so level 1 makes sibling 条 clean
                # sections instead of a growing "第X条 > 第X条" path.
                level = _ARTICLE_LEVEL if has_chapter else 1
                title = marker.replace(" ", "")
                body = text[m.end():].lstrip(" *　").rstrip()
                out.append(Block(f"{b.block_id}h", "title", title, b.page, order, level))
                order += 1
                if body:
                    out.append(Block(f"{b.block_id}b", "paragraph", body, b.page, order))
                    order += 1
        else:
            out.append(Block(b.block_id, b.type, text, b.page, order))
            order += 1
    return out
