"""Parse-stage output / split-stage input contract.

An external document becomes an ordered list of :class:`Block`: plain text
lines by default, or heading-aware blocks when the remote returns markdown.
The split ladder only needs line order plus heading levels — layout details
(fonts, coordinates) are irrelevant to clause segmentation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass
class Block:
    """One atomic content block in reading order.

    Attributes:
        block_id: Unique id within the document.
        type: ``title`` / ``paragraph`` / ``table`` / ``header`` / ``footer`` /
            ``page_number`` (kept for compatibility with remote parsers that
            report layout types).
        text: Block text; tables keep their markdown form.
        page: 1-based page number (0 when unknown).
        order: 0-based position in the document's reading order.
        level: Heading level (1 = top) for ``title`` blocks, else ``None``.
    """

    block_id: str
    type: str
    text: str
    page: int
    order: int
    level: int | None = None


# Types that are pure page furniture, never chunk text.
META_TYPES = frozenset(
    {
        "header",
        "footer",
        "page_number",
        "page_header",
        "page_footer",
        "page_aside_text",
        "page_footnote",
    }
)

CONTENT_TYPES = ("excel", "text", "title", "image")

_COARSE_TYPE = {
    "table": "excel",
    "title": "title",
    "image": "image",
    "chart": "image",
}


def coarse_type(native: str) -> str:
    """Project one native block type onto the coarse vocabulary."""
    return _COARSE_TYPE.get(native, "text")


def coarse_types(native_types: Iterable[str]) -> list[str]:
    """Project native types onto the coarse vocabulary (deduped, prioritized)."""
    found = {coarse_type(t) for t in native_types}
    return [t for t in CONTENT_TYPES if t in found]


def blocks_from_text(text: str) -> list[Block]:
    """One paragraph block per non-empty line.

    The split ladder works line-by-line (same behavior as the previous plain
    text path): heading regexes match a whole line, and the chunker keeps body
    lines under the heading that precedes them.
    """
    return [
        Block(f"t{i}", "paragraph", line, 1, i)
        for i, line in enumerate(line.strip() for line in text.splitlines())
        if line
    ]


# ── markdown → blocks ───────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def blocks_from_markdown(text: str) -> list[Block]:
    """Heading-aware blocks from markdown.

    ``#``..``######`` become ``title`` blocks with their level; contiguous
    ``| ... |`` rows become one ``table`` block; everything else is one
    paragraph block per non-empty line (preserving the line-based split).
    """
    blocks: list[Block] = []
    order = 0
    in_fence = False
    table: list[str] = []

    def flush_table() -> None:
        nonlocal order
        if table:
            blocks.append(Block(f"t{order}", "table", "\n".join(table), 1, order))
            order += 1
            table.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if _FENCE_RE.match(line):
            flush_table()
            in_fence = not in_fence
            continue
        if not line:
            flush_table()
            continue
        if in_fence:
            blocks.append(Block(f"t{order}", "paragraph", line, 1, order))
            order += 1
            continue
        if line.startswith("|") and line.count("|") >= 2:
            if not _is_table_separator(line):
                table.append(line)
            continue
        flush_table()
        heading = _HEADING_RE.match(line)
        if heading:
            level = min(len(heading.group(1)), 6)
            blocks.append(Block(f"t{order}", "title", heading.group(2), 1, order, level))
        else:
            blocks.append(Block(f"t{order}", "paragraph", line, 1, order))
        order += 1
    flush_table()
    return blocks


def _is_table_separator(line: str) -> bool:
    cells = [c.strip() for c in line.strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)
