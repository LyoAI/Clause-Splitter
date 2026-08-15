"""Regulation-structure pass: 编/章/节/条 markers → hierarchy-aware headings.

Legal documents express their hierarchy with markers that are *not* Word
heading styles: 第一章 / 第一节 / 第一条, hand-bolded or plain, all in the
``Normal`` paragraph style. MinerU therefore emits them as plain ``paragraph``
blocks and cannot recover the structure by itself. ``infer_regulation_headings``
is an additive pre-pass that restores it:

- The document's own marker vocabulary (which of 编/章/节/条 appear) fixes the
  heading levels by *relative depth*: 章-only → 章=1, 条=2; 编章节 → 编=1,
  章=2, 节=3, 条=4. Levels are strictly increasing so the chunker's section
  path stays clean (sibling 条 pop each other — no "第X条 > 第X条" chains).
- 条 always opens its own chunk (**unified per-article granularity**): the
  external regulation is compared clause-by-clause, so the internal library is
  indexed clause-by-clause too, with the chapter path preserved in
  ``meta.section`` (e.g. "第一章　总则 > 第一条").
- A document that already has real MinerU headings is *augmented*, not
  skipped: marker ``title`` blocks are re-leveled and marker paragraphs are
  still split into heading + body, so a docx whose 章 use heading styles but
  whose 条 are plain paragraphs still indexes per-条.
- A regulation numbered with Chinese ordinals (一、二、三、) instead of 第X条
  falls back to treating each ordinal line as an article heading.
- The document title (first line, short, punctuation-free, not a marker)
  becomes a level-1 heading that the chunker drops as bare, so no one-line
  retrieval unit is kept for the title.

Non-regulation documents (no marker at all) pass through unchanged.
"""

from __future__ import annotations

import re

from .blocks import Block

# Opening regulation marker, e.g. ``第一章`` / ``第一节`` / ``第一条`` / ``第一编``.
# ``\s*`` around the numeral tolerates sloppy hand-typing ("第 一 条"); ``\d``
# covers Arabic-numeral styles ("第1条"). 款 / 项 are deliberately excluded:
# they are *within*-article sub-units and must stay inside the 条 chunk.
_MARKER_RE = re.compile(r"^\s*\**\s*(第\s*[一二三四五六七八九十百零两\d]+\s*[编章节条])")
# Marker kinds in document order — the legal hierarchy.
_MARKER_KINDS = ("编", "章", "节", "条")

# Chinese-ordinal article marker (一、二、三、…), used only when the document has
# no 第X条 markers at all.
_DUNHAO_RE = re.compile(r"^\s*\**\s*([一二三四五六七八九十]{1,3})、")

# A short, punctuation-free opening line that is not a regulation heading —
# the document title (e.g. "网络数据安全管理条例").
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
    block (computed level) plus its body; marker ``title`` blocks (MinerU real
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
        # The document title (first line, short, punctuation-free, not a marker)
        # becomes a level-1 heading: the chunker drops it as a bare heading the
        # moment the first real chapter/article opens, so the library has no
        # noisy one-line retrieval unit for the title.
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
            # MinerU title blocks as for marker paragraphs.
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
