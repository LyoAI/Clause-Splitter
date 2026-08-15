"""Tests for the regulation-structure pass (``headings.infer_regulation_headings``).

Hand-written internal regulations bold the article markers (``**第一条**　…``)
but keep every paragraph in the ``Normal`` style, so MinerU emits no ``title``
blocks. ``infer_regulation_headings`` re-interprets such paragraph blocks as
hierarchy-aware headings so the chunker groups per-条 (one retrieval unit per
article) with the chapter path kept as ``meta.section`` context.

Design under test:
- The document's own marker vocabulary (编 / 章 / 节 / 条) fixes heading levels
  by *relative depth*: 章-only → 章=1, 条=2; 编章节条 → 编=1, 章=2, 节=3, 条=4.
  Levels are strictly increasing so sibling 条 pop each other on the section
  path ("第一章　总则 > 第一条", never "第X条 > 第Y条").
- 条 always opens its own chunk (unified per-article granularity).
- Documents that already carry MinerU ``title`` blocks are *augmented*, not
  skipped: marker paragraphs are still split into heading + body.
- A regulation numbered 一、二、三、 (no 第X条) falls back to per-ordinal
  article headings.
- The document title (short first line) becomes a bare level-1 heading that
  the chunker drops.
"""

from __future__ import annotations

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


def _sections(chunks) -> list[str]:
    return [c.meta["section"] for c in chunks]


# -------------------------------------------------------------- chapter docs --

def test_chapter_and_article_heads_become_titles() -> None:
    # 章 + 条 present → relative depth: 章=1, 条=2.
    blocks = [
        p("网络数据安全管理条例"),  # doc title
        p("第一章　总则"),
        p("**第一条**　为了规范网络数据处理活动，保障网络数据安全。"),
        p("第二章　一般规定"),
        p("**第八条**　任何个人、组织不得利用网络数据从事非法活动。"),
    ]
    out = infer_regulation_headings(blocks)
    assert _kinds(out) == [
        ("title", "网络数据安全管理条例", 1),      # doc title → level-1 (dropped bare)
        ("title", "第一章　总则", 1),              # 章 → relative depth 1
        ("title", "第一条", 2),                    # 条 → relative depth 2
        ("paragraph", "为了规范网络数据处理活动，保障网络数据安全。", None),
        ("title", "第二章　一般规定", 1),
        ("title", "第八条", 2),
        ("paragraph", "任何个人、组织不得利用网络数据从事非法活动。", None),
    ]


def test_articles_open_own_chunks_with_section_path() -> None:
    # Unified per-条 granularity: every 条 is its own retrieval unit, carrying
    # the chapter path in meta.section.
    blocks = [
        p("第一章　总则"),
        p("**第一条**　甲。"),
        p("**第二条**　乙。"),
        p("第二章　一般规定"),
        p("**第八条**　丙。"),
    ]
    chunks = chunk_doc("内规.docx", infer_regulation_headings(blocks), min_tokens=0)
    assert _sections(chunks) == [
        "第一章　总则 > 第一条",
        "第一章　总则 > 第二条",
        "第二章　一般规定 > 第八条",
    ]
    assert len(chunks) == 3
    assert "甲。" in chunks[0].text and "第一条" in chunks[0].text
    assert "乙。" in chunks[1].text
    assert "丙。" in chunks[2].text


# ------------------------------------------------------------ chapter-less -----

def test_chapter_less_regulation_opens_one_chunk_per_article() -> None:
    blocks = [p("**第一条**　甲。"), p("**第二条**　乙。")]
    out = infer_regulation_headings(blocks)
    # No 编/章/节 → 条 is the document's own top structure (level 1), each
    # opening its own chunk, each followed by its body.
    assert _kinds(out) == [
        ("title", "第一条", 1),
        ("paragraph", "甲。", None),
        ("title", "第二条", 1),
        ("paragraph", "乙。", None),
    ]
    chunks = chunk_doc("内规.docx", out, min_tokens=0)
    assert _sections(chunks) == ["第一条", "第二条"]


# ---------------------------------------------------------- relative depth ----

def test_bian_zhang_jie_tiao_relative_depth() -> None:
    # 编章节条 all present → strictly increasing 编=1, 章=2, 节=3, 条=4.
    blocks = [
        p("第一编　总则"),
        p("第一章　一般规定"),
        p("第一节　数据分类"),
        p("**第一条**　甲。"),
        p("**第二条**　乙。"),
    ]
    out = infer_regulation_headings(blocks)
    assert _kinds(out) == [
        ("title", "第一编　总则", 1),
        ("title", "第一章　一般规定", 2),
        ("title", "第一节　数据分类", 3),
        ("title", "第一条", 4),
        ("paragraph", "甲。", None),
        ("title", "第二条", 4),
        ("paragraph", "乙。", None),
    ]


def test_bian_without_chapter_relative_depth() -> None:
    # 编 present but no 章/节 → 编=1, 条=2 (no holes in the level scale).
    blocks = [p("第一编　总则"), p("**第一条**　甲。")]
    out = infer_regulation_headings(blocks)
    assert _kinds(out) == [
        ("title", "第一编　总则", 1),
        ("title", "第一条", 2),
        ("paragraph", "甲。", None),
    ]


# ------------------------------------------------------------- augmenting -----

def test_augments_real_mineru_titles_with_marker_articles() -> None:
    # A docx whose 章 use real heading styles but whose 条 are plain paragraphs
    # is augmented, not skipped: 章 title re-leveled, 条 paragraphs split.
    blocks = [t("第一章　总则"), p("**第一条**　甲。")]
    out = infer_regulation_headings(blocks)
    assert _kinds(out) == [
        ("title", "第一章　总则", 1),
        ("title", "第一条", 2),
        ("paragraph", "甲。", None),
    ]
    chunks = chunk_doc("内规.docx", out, min_tokens=0)
    assert _sections(chunks) == ["第一章　总则 > 第一条"]


# --------------------------------------------------------------- no-ops ------

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
    assert _sections(chunks) == [
        "第一章　总则 > 第一条",
        "第二章　一般规定 > 第八条",
    ]
    assert not any("网络数据安全管理条例" == c.text.strip() for c in chunks)


def test_heading_body_splitting_keeps_label_and_body() -> None:
    # "**第三条**　处理…" splits into title "第三条" + paragraph body.
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


# ----------------------------------------------------------- dunhao fallback --

def test_dunhao_numbering_fallback() -> None:
    # No 第X条 markers at all → 一、二、三、 lines become per-ordinal article
    # headings (level 1), whole line as the heading, body paragraphs following.
    blocks = [
        p("某公司数据管理制度"),
        p("一、总则"),
        p("本制度适用于公司全体员工。"),
        p("二、数据分类分级"),
        p("数据按敏感程度分为四类。"),
        p("三、安全保护措施"),
        p("应当落实访问控制、加密等安全措施。"),
    ]
    out = infer_regulation_headings(blocks)
    assert _kinds(out) == [
        ("title", "某公司数据管理制度", 1),  # doc title → dropped bare
        ("title", "一、总则", 1),
        ("paragraph", "本制度适用于公司全体员工。", None),
        ("title", "二、数据分类分级", 1),
        ("paragraph", "数据按敏感程度分为四类。", None),
        ("title", "三、安全保护措施", 1),
        ("paragraph", "应当落实访问控制、加密等安全措施。", None),
    ]
    chunks = chunk_doc("内规.docx", out, min_tokens=0)
    assert _sections(chunks) == ["一、总则", "二、数据分类分级", "三、安全保护措施"]


def test_dunhao_not_triggered_when_x_tiao_present() -> None:
    # A document WITH 第X条 must NOT treat its 一、二、 sub-items as headings.
    blocks = [
        p("第一章　总则"),
        p("**第一条**　(一)甲；(二)乙。"),
    ]
    out = infer_regulation_headings(blocks)
    # No dunhao headings introduced — the "（一）" sub-item stays inside the 条 body.
    assert _kinds(out) == [
        ("title", "第一章　总则", 1),
        ("title", "第一条", 2),
        ("paragraph", "(一)甲；(二)乙。", None),
    ]
