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
from typing import Iterable


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


# The coarse, *form-of-content* vocabulary a retrieval caller filters on
# (``RAGTool.search_by_question(recall_content_types=...)``): one value per
# shape a passage can take, in priority order.
#   excel — tabular (an xlsx sheet, a markdown table, a table detected in a PDF)
#   text  — prose
#   title — a heading
#   image — a caption for visual content (figure, chart)
# It describes document form, not subject matter, so it holds for any corpus.
# MinerU's native taxonomy stays available alongside it (``Chunk.meta
# ["native_types"]``) for a caller that needs finer granularity.
CONTENT_TYPES = ("excel", "text", "title", "image")

# MinerU native type -> coarse type. Types absent here are prose: whatever the
# parser emits as readable content (``paragraph`` / ``text`` / ``list`` /
# ``index`` / ``equation`` / ``code`` / ...) unless it is explicitly tabular or
# visual. Page furniture never reaches this mapping — the chunker absorbs
# ``META_TYPES`` before any content is recorded.
_COARSE_TYPE = {
    "table": "excel",
    "title": "title",
    "image": "image",
    "chart": "image",
}


def coarse_type(native: str) -> str:
    """Project one MinerU native block type onto the coarse vocabulary."""
    return _COARSE_TYPE.get(native, "text")


def coarse_types(native_types: Iterable[str]) -> list[str]:
    """Project native types onto the coarse vocabulary.

    Deduped and ordered by ``CONTENT_TYPES`` priority, so the result is
    deterministic regardless of the order blocks were read in.
    """
    found = {coarse_type(t) for t in native_types}
    return [t for t in CONTENT_TYPES if t in found]
