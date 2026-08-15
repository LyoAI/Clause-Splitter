"""Tests for the regulation-heading fallback (``headings.infer_regulation_headings``).

Hand-written internal regulations bold the article markers (``**第一条**　…``)
but keep every paragraph in the ``Normal`` style, so MinerU emits no ``title``
blocks. ``infer_regulation_headings`` re-interprets such paragraph blocks as
headings so the chunker can group by chapter / section again instead of
collapsing the whole regulation into one chunk.

Cases covered:
- 章 / 节 headings become level-1/2 title blocks (they open chunks).
- 条 headings become level-4 titles (stay intra-chunk) when chapters exist;
  level-3 (open their own chunk) when the regulation has no chapters.
- A short punctuation-free first line (the document title) becomes a level-1
  heading so the chunker drops it as a bare heading (no orphan retrieval unit).
- No-op when MinerU already found ``title`` blocks, or when nothing matches a
  regulation-heading pattern.
- Headings are split from their body; the article label stays as a ``title``
  block and the body becomes a ``paragraph``.
"""

from __future__ import annotations

from clause_splitter.config import CHUNK_LEVEL
from clause_splitter.tools.parser import chunk_doc
from clause_splitter.tools.parser.blocks import Block
from clause_splitter.tools.parser.headings import infer_regulation_headings


def p(text: str, block_id: str = "p") -> Block:
    return Block(block_id=block_id, type="paragraph", text=text, page=1, order=0)


def t(text: str) -> Block:
    return Block(block_id="t", type="title", text=text, page=1, order=0, level=1)


def _kinds(blocks: list[Block]) -> list[tuple[str, str, int | None]]:
    """(type, text, level) for each block — the shape that drives the chunker."""
    return [(b.type, b.text, b.level) for b in blocks]


# -------------------------------------------------------------- chapter docs --


def test_chapter_and_article_heads_become_titles() -> None:
    blocks = [
        p("网络数据安全管理条例"),  # doc title
        p("第一章　总则"),
        p("**第一条**　为了规范网络数据处理活动，保障网络数据安全。"),
        p("第二章　一般规定"),
        p("**第八条**　任何个人、组织不得利用网络数据从事非法活动。"),
    ]
    out = infer_regulation_headings(blocks)
    assert _kinds(out) == [
        ("title", "网络数据安全管理条例", 1),       # doc title → level-1 (dropped bare)
        ("title", "第一章　总则", 1),               # 章 opens a chunk
        ("title", "第一条", CHUNK_LEVEL + 1),       # 条 stays intra-chunk
        ("paragraph", "为了规范网络数据处理活动，保障网络数据安全。", None),
        ("title", "第二章　一般规定", 1),
        ("title", "第八条", CHUNK_LEVEL + 1),
        ("paragraph", "任何个人、组织不得利用网络数据从事非法活动。", None),
    ]


def test_articles_aggregate_into_chapter_chunks() -> None:
    # With chapters present, 条 never opens its own chunk: several articles
    # under one 章 fold into a single chapter-level retrieval unit.
    blocks = [
        p("第一章　总则"),
        p("**第一条**　甲。"),
        p("**第二条**　乙。"),
        p("第二章　一般规定"),
        p("**第八条**　丙。"),
    ]
    chunks = chunk_doc("内规.docx", infer_regulation_headings(blocks), min_tokens=0)
    assert [c.meta["section"] for c in chunks] == ["第一章　总则", "第二章　一般规定"]
    assert len(chunks) == 2
    assert "甲。" in chunks[0].text and "乙。" in chunks[0].text  # 两条并一章
    assert "丙。" in chunks[1].text


# ------------------------------------------------------------ chapter-less -----


def test_chapter_less_regulation_opens_one_chunk_per_article() -> None:
    blocks = [p("**第一条**　甲。"), p("**第二条**　乙。")]
    out = infer_regulation_headings(blocks)
    # No 章 in the document → 条 headings become the document's own top
    # structure (level 1), each opening its own chunk, each followed by its body.
    assert _kinds(out) == [
        ("title", "第一条", 1),
        ("paragraph", "甲。", None),
        ("title", "第二条", 1),
        ("paragraph", "乙。", None),
    ]
    chunks = chunk_doc("内规.docx", out, min_tokens=0)
    assert [c.meta["section"] for c in chunks] == ["第一条", "第二条"]


# ---------------------------------------------------------------- no-ops ------


def test_noop_when_mineru_found_title_blocks() -> None:
    blocks = [t("第一章　总则"), p("**第一条**　甲。")]
    assert infer_regulation_headings(blocks) is blocks  # unchanged, same object


def test_noop_when_no_heading_pattern_matches() -> None:
    blocks = [p("这是普通规章制度文本，没有条文章节编号。"), p("继续说明。")]
    out = infer_regulation_headings(blocks)
    assert _kinds(out) == [("paragraph", blocks[0].text, None), ("paragraph", blocks[1].text, None)]
    assert out == blocks  # same content


def test_noop_when_first_block_is_verbose_not_a_title() -> None:
    # A preamble that is long / punctuated must NOT be reclassified as the title.
    preamble = "为加强网络数据安全管理，规范网络数据处理活动，保障网络数据安全，制定本条例。"
    blocks = [p(preamble), p("第一章　总则"), p("**第一条**　甲。")]
    out = infer_regulation_headings(blocks)
    assert (out[0].type, out[0].text) == ("paragraph", preamble)


# -------------------------------------------------------- doc-title handling --


def test_doc_title_becomes_bare_level1_heading_and_is_dropped() -> None:
    # "网络数据安全管理条例" is the first line, short and punctuation-free →
    # level-1 title. When the first 章 arrives the chunker drops the title chunk
    # as a bare heading: no one-line retrieval unit for the title.
    blocks = [
        p("网络数据安全管理条例"),
        p("第一章　总则"),
        p("**第一条**　甲。"),
        p("第二章　一般规定"),
        p("**第八条**　乙。"),
    ]
    chunks = chunk_doc("内规.docx", infer_regulation_headings(blocks), min_tokens=0)
    assert [c.meta["section"] for c in chunks] == ["第一章　总则", "第二章　一般规定"]
    assert not any("网络数据安全管理条例" == c.text.strip() for c in chunks)


def test_heading_body_splitting_keeps_label_and_body() -> None:
    # "**第一条**　为了…" splits into title "第一条" + paragraph body.
    blocks = [p("**第三条**　处理个人信息应当遵循合法、正当、必要原则。")]
    out = infer_regulation_headings(blocks)
    assert _kinds(out) == [
        ("title", "第三条", 1),
        ("paragraph", "处理个人信息应当遵循合法、正当、必要原则。", None),
    ]


def test_marker_with_space_normalized() -> None:
    # "第 一 条" (spaces inside the marker) normalizes to "第一条".
    blocks = [p("**第 一 条**　甲。")]
    out = infer_regulation_headings(blocks)
    assert out[0].text == "第一条"
