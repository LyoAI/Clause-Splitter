"""Regulation-structure pass: 编/章/节/条 markers → hierarchy-aware headings.

Parsers emit regulation markers as plain paragraphs (they are rarely Word
heading styles), so this pre-pass restores levels by relative depth. 条 always
opens its own chunk, keeping the chapter path in the section metadata; 编/章/节
only provide context. Documents with no marker pass through unchanged.
"""

from __future__ import annotations

import re

from clause_splitter.parsing.blocks import Block

# Opening regulation marker; spaces and Arabic numerals are tolerated. 款/项 are
# excluded — they belong inside the article chunk.
_MARKER_RE = re.compile(r"^\s*\**\s*(第\s*[一二三四五六七八九十百零两\d]+\s*[编章节条])")
# Marker kinds in document order — the legal hierarchy.
_MARKER_KINDS = ("编", "章", "节", "条")

# Chinese-ordinal article marker, used only when no 第X条 marker exists.
_DUNHAO_RE = re.compile(r"^\s*\**\s*([一二三四五六七八九十]{1,3})、")

# Short, punctuation-free opening line — the document title.
_DOC_TITLE_RE = re.compile(r"^[^。！？；;:：,，]{1,40}$")


def _looks_like_doc_title(text: str) -> bool:
    return bool(text) and _DOC_TITLE_RE.match(text) and not _MARKER_RE.match(text) and not _DUNHAO_RE.match(text)


def _marker_levels(blocks: list[Block]) -> dict[str, int] | None:
    """Relative-depth heading levels for the markers present, or None when the
    document is not regulation-shaped (no marker at all)."""
    seen: set[str] = set()
    for b in blocks:
        m = _MARKER_RE.match(b.text.strip())
        if m:
            seen.add(m.group(1)[-1])
    if not seen:
        return None
    levels: dict[str, int] = {}
    for kind in _MARKER_KINDS:
        if kind in seen:
            levels[kind] = len(levels) + 1
    return levels


def infer_regulation_headings(blocks: list[Block]) -> list[Block]:
    """Re-classify regulation markers as hierarchy-aware headings.

    Returns ``blocks`` unchanged for non-regulation documents. For
    regulation-shaped documents, marker paragraphs are split into a ``title``
    block (computed level) plus its body; marker ``title`` blocks (parser real
    headings) are re-leveled to the same scale so section paths agree. A 条
    always opens its own chunk; 编 / 章 / 节 provide the section-path context.
    """
    blocks = list(blocks)
    levels = _marker_levels(blocks)
    if levels is None:
        # Not a 第X章/节/条 document — maybe it numbers articles 一、二、三、.
        if any(_DUNHAO_RE.match(b.text.strip()) for b in blocks):
            return _classify_dunhao(blocks)
        return blocks  # not a regulation-shaped document.

    out: list[Block] = []
    order = 0
    for i, b in enumerate(blocks):
        text = b.text.strip()
        # Title → level-1 heading; the chunker drops it when content opens.
        if i == 0 and b.type == "paragraph" and _looks_like_doc_title(text):
            out.append(Block(b.block_id, "title", text, b.page, order, 1))
            order += 1
            continue
        m = _MARKER_RE.match(text)
        if not m:
            out.append(Block(b.block_id, b.type, text, b.page, order))
            order += 1
            continue
        level = levels[m.group(1)[-1]]
        if m.group(1)[-1] == "条":
            # 条: split into heading + body so the article label anchors the
            # chunk text (and the section path) while the body feeds retrieval.
            title = m.group(1).replace(" ", "")
            body = text[m.end():].lstrip(" *　").rstrip()
            out.append(Block(f"{b.block_id}h", "title", title, b.page, order, level))
            order += 1
            if body:
                out.append(Block(f"{b.block_id}b", "paragraph", body, b.page, order))
                order += 1
        else:
            # 编 / 章 / 节: the whole line is the heading (第一章　总则). Same for
            # parser title blocks as for marker paragraphs.
            title = text.strip("* ").strip()
            out.append(Block(b.block_id, "title", title, b.page, order, level))
            order += 1
    return out


def _classify_dunhao(blocks: list[Block]) -> list[Block]:
    """一、二、三、… numbering → one article heading per ordinal line (level 1).

    Unlike a bare 第X条 label, the text after the ordinal is itself the article
    title ("一、数据分类分级"), so the whole line becomes the heading rather than
    being split into label + body.
    """
    out: list[Block] = []
    order = 0
    for i, b in enumerate(blocks):
        text = b.text.strip()
        if i == 0 and b.type == "paragraph" and _looks_like_doc_title(text):
            out.append(Block(b.block_id, "title", text, b.page, order, 1))
            order += 1
            continue
        if b.type == "paragraph" and _DUNHAO_RE.match(text):
            title = text.strip("* ").strip()
            out.append(Block(b.block_id, "title", title, b.page, order, 1))
            order += 1
        else:
            out.append(Block(b.block_id, b.type, text, b.page, order))
            order += 1
    return out
