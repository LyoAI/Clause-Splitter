"""Parse-stage interface: markdown documents → blocks (DocFinQA / hand-written).

DocFinQA ships each SEC filing as one markdown string (its own HTML→markdown
conversion), so the MinerU parse stage is not needed: this module is the
markdown ↔ ``Block`` projection that feeds the same ``chunk_doc`` /
``doc_sections`` pipeline as ``mineru_parse``. The taxonomy it emits is the same
MinerU-shaped one (``title`` / ``paragraph`` / ``table``), so every downstream
stage is reused unchanged.

SEC text has no pages, so ``Block.page`` is the constant 1; ``order`` carries
the reading order instead, which is what the chunker actually consumes. ``level``
is the heading depth for ``title`` blocks (1 = top), exactly as ``_opens_section``
expects — so heading hierarchy from the markdown maps onto chunk boundaries and
the TOC identically to a MinerU-parsed document.
"""

from __future__ import annotations

import re

from .blocks import Block

# ATX headings ``# .. ######``; trailing closing hashes allowed (``## Title ##``).
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# Setext underline ``===`` (h1) / ``---`` (h2), alone on a line.
_SETEXT_UNDERLINE = re.compile(r"^\s*(={3,}|-{3,})\s*$")
# Horizontal rule ``---`` / ``***`` / ``___`` (3+ repeated chars, alone).
_HRULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
# GFM table separator row: only ``| - : `` characters, with at least one ``-``.
_SEPARATOR_ROW = re.compile(r"^\s*\|?[\s|:-]+\|?\s*$")


def parse_markdown(text: str) -> list[Block]:
    """Project markdown ``text`` onto MinerU-shaped ``Block`` lists.

    ATX ``#`` headings become ``title`` blocks (``level`` = heading depth);
    setext headings are folded in. GFM tables (a run of ``|``-lines with a
    separator row) and raw ``<table>`` blocks stay whole as ``table`` blocks —
    never re-typed, so the chunker keeps them atomic. Everything else is a
    ``paragraph``. Horizontal rules / comments / blank lines are dropped. Page
    is always 1.
    """
    lines = text.splitlines()
    blocks: list[Block] = []
    order = 0
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # ATX heading.
        m = _ATX_HEADING.match(line)
        if m:
            blocks.append(_mk("title", m.group(2).strip(), order, level=len(m.group(1))))
            order += 1
            i += 1
            continue

        # GFM table: a run of consecutive ``|``-lines containing a separator row.
        if _starts_table(lines, i):
            j = i
            while j < n and _table_line(lines[j]):
                j += 1
            blocks.append(_mk("table", "\n".join(lines[i:j]).strip(), order))
            order += 1
            i = j
            continue

        # Raw HTML table block (DocFinQA keeps some markup verbatim).
        if line.startswith("<table"):
            j = i
            while j < n and "</table>" not in lines[j]:
                j += 1
            j = min(j + 1, n)  # consume the closing tag line too
            blocks.append(_mk("table", "\n".join(lines[i:j]).strip(), order))
            order += 1
            i = j
            continue

        # Horizontal rule / comment.
        if _HRULE.match(line) or line.startswith("<!--"):
            i += 1
            continue

        # Setext heading: ``text`` on this line + an underline on the next.
        if i + 1 < n and _SETEXT_UNDERLINE.match(lines[i + 1].strip()):
            level = 1 if lines[i + 1].startswith("=") else 2
            blocks.append(_mk("title", line, order, level=level))
            order += 1
            i += 2
            continue

        # Paragraph: gather until a blank line or a new structural element.
        j = i + 1
        buf = [line]
        while j < n:
            nxt = lines[j].strip()
            if not nxt or _structural_start(lines, j, n):
                break
            buf.append(nxt)
            j += 1
        blocks.append(_mk("paragraph", "\n".join(buf), order))
        order += 1
        i = j
    return blocks


def parse_markdown_file(path) -> list[Block]:
    """Parse a ``.md`` file on disk (UTF-8) into blocks."""
    with open(path, encoding="utf-8") as f:
        return parse_markdown(f.read())


def _mk(kind: str, text: str, order: int, *, level: int | None = None) -> Block:
    return Block(block_id=f"b{order}", type=kind, text=text, page=1, order=order, level=level)


def _table_line(line: str) -> bool:
    """A GFM table row: starts with ``|`` (allow leading spaces)."""
    s = line.strip()
    return s.startswith("|") and "|" in s[1:]


def _is_separator_row(line: str) -> bool:
    """A GFM separator row: only ``| - : `` characters, containing a ``-``."""
    return bool(_SEPARATOR_ROW.match(line)) and "-" in line


def _starts_table(lines: list[str], i: int) -> bool:
    """True when ``lines[i:]`` is a GFM table — a ``|`` run with a separator row.

    Requires the separator row so a lone ``| … |`` prose line stays a paragraph.
    """
    if not _table_line(lines[i]):
        return False
    n = len(lines)
    j = i
    while j < n and _table_line(lines[j]):
        if _is_separator_row(lines[j]):
            return True
        j += 1
    return False


def _structural_start(lines: list[str], j: int, n: int) -> bool:
    """True when ``lines[j]`` begins a block the paragraph gather must stop at."""
    line = lines[j].strip()
    if _ATX_HEADING.match(line) or _starts_table(lines, j) or line.startswith("<table"):
        return True
    # ``text`` + an underline on the next line = a setext heading boundary.
    if j + 1 < n and _SETEXT_UNDERLINE.match(lines[j + 1].strip()):
        return True
    return _HRULE.match(line) or line.startswith("<!--")
