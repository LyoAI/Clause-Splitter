"""Parse-stage output / chunk-stage input contract.

MinerU's layout-detection model classifies each region of a page into a content
type (``title`` / ``paragraph`` / ``text`` / ``table`` / ``equation`` / ``image``
/ ``header`` / ``footer`` / ``page_number`` / ...). We keep that native taxonomy
in ``Block.type`` rather than collapsing to a three-state model, and only add the
two fields the chunker needs beyond what MinerU emits: ``page`` (1-based) and
``order`` (reading order). The chunker decides which types contribute text and
which ones are structural signals.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Block:
    """One atomic layout block from document parsing (MinerU-shaped).

    Attributes:
        block_id: Globally unique id within the document.
        type: MinerU content type — ``title`` / ``paragraph`` / ``text`` /
            ``table`` / ``image`` / ``chart`` / ``list`` / ``index`` /
            ``equation`` / ``header`` / ``footer`` / ``page_number``.
        text: Markdown content; tables keep their markdown form.
        page: 1-based page number (MinerU's ``page_idx`` is 0-based; +1 here).
        order: 0-based position in the document's reading order.
        level: Heading level (1 = top) for ``title`` blocks, else ``None``.
    """

    block_id: str
    type: str
    text: str
    page: int
    order: int
    level: int | None = None


# Types that are pure metadata (page furniture), never chunk text. Both the
# v1 spellings (``header`` / ``footer``) and the v2 spellings
# (``page_header`` / ``page_footer`` / ``page_aside_text`` / ``page_footnote``)
# are possible, depending on which MinerU content_list schema was dumped.
# Everything else contributes chunk text.
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
