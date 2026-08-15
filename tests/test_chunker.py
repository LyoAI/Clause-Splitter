"""Tests for hierarchy-aware chunking (``chunker.chunk_doc``).

Covers the approved design: level-≤3 headings open chunks and drive the section
path; deeper sub-headings stay in the chunk; tables stay embedded in their
section and are never split; page furniture goes to ``meta``, never text; the
resulting ``meta`` carries page / pages / section / table_ids.
"""

from __future__ import annotations

import pytest

from clause_splitter.tools.parser import Block, chunk_doc, doc_sections, estimate_tokens
from clause_splitter.tools.parser import chunker


@pytest.fixture(autouse=True)
def heuristic_tokens(monkeypatch):
    # The real default token counter loads the embedding model's tokenizer (and
    # may download it); these deterministic assertions pin the heuristic.
    monkeypatch.setattr(chunker, "DEFAULT_TOKEN_COUNT", chunker.estimate_tokens)


def h(level: int, text: str, page: int = 1) -> Block:
    return Block(block_id=f"h{level}", type="title", text=text, page=page, order=0, level=level)


def p(text: str, page: int = 1) -> Block:
    return Block(block_id="p", type="paragraph", text=text, page=page, order=0)


def t(text: str, page: int = 1) -> Block:
    return Block(block_id="t", type="table", text=text, page=page, order=0)


def idx(text: str, page: int = 1) -> Block:
    return Block(block_id="idx", type="index", text=text, page=page, order=0)


def hdr(text: str, page: int = 1) -> Block:
    return Block(block_id="hdr", type="page_header", text=text, page=page, order=0)


def ftr(text: str, page: int = 1) -> Block:
    return Block(block_id="ftr", type="page_footer", text=text, page=page, order=0)


# ------------------------------------------------------------------- sections --


def test_heading_level_3_opens_chunks_and_builds_section_path() -> None:
    blocks = [
        h(1, "第一章 公司概况"),
        p("郑州银行是河南省内城商行。"),
        h(2, "公司简介"),
        p("成立于1996年。"),
        h(3, "经营情况"),
        p("营业收入稳步增长。"),
        h(2, "公司治理"),
        p("董事会构成如下。"),
    ]
    chunks = chunk_doc("doc.pdf", blocks)
    assert [c.meta["section"] for c in chunks] == [
        "第一章 公司概况",
        "第一章 公司概况 > 公司简介",
        "第一章 公司概况 > 公司简介 > 经营情况",
        "第一章 公司概况 > 公司治理",
    ]
    # sibling level-2 heading popped the previous level-2 branch off the stack.


def test_sub_heading_below_level_stays_in_chunk() -> None:
    blocks = [
        h(1, "财务报告"),
        h(3, "合并资产负债表"),
        h(4, "流动资产"),
        p("货币资金余额较高。"),
        h(4, "非流动资产"),
        p("固定资产占比低。"),
    ]
    chunks = chunk_doc("doc.pdf", blocks)
    # level-4 subtitles are not boundaries: heading + subtitle + paragraph live
    # in one chunk so the granularity never gets too fine.
    assert len(chunks) == 1
    assert chunks[0].meta["section"] == "财务报告 > 合并资产负债表"
    assert "流动资产" in chunks[0].text and "非流动资产" in chunks[0].text


def test_bare_heading_without_body_is_not_emitted() -> None:
    blocks = [h(1, "财务报告"), h(2, "合并资产负债表"), p("正文内容。")]
    chunks = chunk_doc("doc.pdf", blocks)
    assert len(chunks) == 1
    assert chunks[0].meta["section"] == "财务报告 > 合并资产负债表"
    assert "正文内容。" in chunks[0].text


def test_doc_sections_emits_toc_for_boundary_headings() -> None:
    blocks = [
        h(1, "财务报告", page=40),
        h(4, "流动资产"),  # below CHUNK_LEVEL → not a TOC entry
        p("货币资金余额较高。"),
        h(3, "合并资产负债表", page=41),
        p("以下为期末资产情况。"),
        h(2, "公司治理", page=50),
        p("董事会构成如下。"),
        p("无标题内容。"),  # plain paragraph → not a TOC entry
    ]
    sections = doc_sections("doc.pdf", blocks)
    assert [(s.title, s.level, s.page) for s in sections] == [
        ("财务报告", 1, 40),
        ("合并资产负债表", 3, 41),
        ("公司治理", 2, 50),
    ]
    assert [s.section_id for s in sections] == ["doc.pdf#s0", "doc.pdf#s1", "doc.pdf#s2"]


def test_doc_sections_prefers_toc_index_block_with_page_offset() -> None:
    # The printed 目錄 ("釋義 2") wins over title detection: offset +2 maps the
    # printed page to the physical page of the first matching heading.
    blocks = [
        h(1, "目 錄", page=3),
        idx("- 释义 2\n- 公司简介 4\n- 会计数据及财务指标概要 6", page=3),
        h(2, "释义", page=4),
        p("「本行」指天津银行。"),
        h(2, "公司简介", page=6),
        p("本行成立于1996年。"),
        h(2, "会计数据及财务指标概要", page=8),
        t("| 年份 | 收入 |"),
    ]
    sections = doc_sections("doc.pdf", blocks)
    assert [(s.title, s.level, s.page) for s in sections] == [
        ("释义", 1, 4),
        ("公司简介", 1, 6),
        ("会计数据及财务指标概要", 1, 8),
    ]
    assert [s.section_id for s in sections] == ["doc.pdf#s0", "doc.pdf#s1", "doc.pdf#s2"]


def test_doc_sections_falls_back_when_index_not_a_toc() -> None:
    # An appendix "index" whose lines are not "title page" (address + postal
    # code) must not become the TOC; the boundary-heading fallback is used.
    blocks = [
        h(2, "财务报告", page=10),
        p("正文内容。"),
        idx("- 序號 機構名稱 機構地址 郵編\n- 17 天津銀行雍陽西道支行 天津市武清區 301799", page=20),
    ]
    sections = doc_sections("doc.pdf", blocks)
    assert [(s.title, s.level, s.page) for s in sections] == [("财务报告", 2, 10)]


def test_doc_sections_skips_empty_heading_and_matches_chunk_paths() -> None:
    # Empty-text boundary headings are not sections, and the TOC's boundary
    # predicate is the same one that opens chunks (the section paths agree).
    blocks = [h(1, "财务报告"), h(2, "   "), h(3, "合并资产负债表"), p("正文内容。")]
    sections = doc_sections("doc.pdf", blocks)
    assert [s.title for s in sections] == ["财务报告", "合并资产负债表"]
    chunks = chunk_doc("doc.pdf", blocks)
    body = next(c for c in chunks if "正文内容。" in c.text)
    assert body.meta["section"] == "财务报告 > 合并资产负债表"


# -------------------------------------------------------------------- tables --


def test_table_embedded_in_section() -> None:
    blocks = [h(2, "合并资产负债表"), p("以下为期末资产情况。"), t("| 总资产 | 6,386.92 亿元 |"), p("附注见后。")]
    chunks = chunk_doc("doc.pdf", blocks)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.meta["section"] == "合并资产负债表"
    assert c.meta["table_ids"] == ["T1"]
    assert "| 总资产" in c.text


def test_table_ids_number_in_reading_order() -> None:
    blocks = [h(1, "财务报告"), t("表甲"), h(1, "风险"), t("表乙")]
    chunks = chunk_doc("doc.pdf", blocks)
    assert chunks[0].meta["table_ids"] == ["T1"]
    assert chunks[1].meta["table_ids"] == ["T2"]


def test_overflow_table_travels_with_lead_in() -> None:
    # A section that fills the budget and then meets an oversized table: the cut
    # must land before the table's lead-in, never through the table.
    big = "字" * 200  # ~200 tokens
    lead = "导读段落" + "字" * 40
    table = "| 甲 | 乙 |\n| --- | --- |\n" + "| 1 | 2 |\n" * 80
    blocks = [h(1, "财务报告"), p(big), p(lead), t(table)]
    chunks = chunk_doc("doc.pdf", blocks, chunk_size=300)
    table_chunks = [c for c in chunks if "| 1 | 2 |" in c.text]
    assert len(table_chunks) == 1  # table never split
    assert "导读段落" in table_chunks[0].text  # lead-in travels with the table
    assert table_chunks[0].meta["table_ids"] == ["T1"]


def test_giant_table_gets_own_chunk() -> None:
    # A single table over the budget is atomic: its own chunk, never split.
    huge = "| 项 | 值 |\n| --- | --- |\n" + "| 1 | 2 |\n" * 500
    blocks = [h(2, "母公司资产负债表"), p("导读文字。"), t(huge)]
    chunks = chunk_doc("doc.pdf", blocks, chunk_size=300)
    table_chunks = [c for c in chunks if "| 1 | 2 |" in c.text]
    assert len(table_chunks) == 1
    assert table_chunks[0].meta["section"] == "母公司资产负债表"
    assert "导读文字。" in chunks[0].text  # lead-in stays in the preceding chunk
    assert chunks[0].meta.get("table_ids") in (None, [])


def test_non_table_overflow_splits_at_block_boundary() -> None:
    blocks = [h(1, "财务报告"), p("甲" * 120), p("乙" * 120), p("丙" * 30)]
    chunks = chunk_doc("doc.pdf", blocks, chunk_size=150)
    # Three paragraphs under one heading: the first two each exceed the budget
    # as a single unit; the cut always lands between blocks, never mid-block.
    assert all("甲" * 120 in c.text or "乙" * 120 in c.text or "丙" * 30 in c.text for c in chunks)
    assert "".join(c.text for c in chunks).count("甲乙丙") == 0
    assert all(c.meta["section"] == "财务报告" for c in chunks)


# ---------------------------------------------- heading-less / oversized ----


def test_headingless_doc_uses_doc_id_as_section() -> None:
    # No headings at all → the doc id becomes the section anchor.
    blocks = [p("货币资金余额较高。"), p("固定资产占比低。")]
    chunks = chunk_doc("公司.docx", blocks)
    assert len(chunks) == 1
    assert chunks[0].meta["section"] == "公司.docx"


def test_headingless_doc_split_keeps_doc_id_section() -> None:
    blocks = [p("甲" * 60), p("乙" * 60)]
    chunks = chunk_doc("公司.xlsx", blocks, chunk_size=70)
    assert len(chunks) == 2
    assert all(c.meta["section"] == "公司.xlsx" for c in chunks)


def test_oversized_prose_splits_at_sentence_boundaries() -> None:
    # ~12 tokens/sentence, total ~60 well over the 25-token budget. Splits must
    # land between sentences and preserve order exactly.
    para = "".join(f"这是第{i}句，金额为{i * 100}万元。" for i in range(1, 6))
    blocks = [p(para)]
    # min_tokens=0: this test verifies the split, not the merge pass.
    chunks = chunk_doc("doc.pdf", blocks, chunk_size=25, min_tokens=0)
    assert len(chunks) >= 2
    assert "".join(c.text for c in chunks) == para  # nothing lost or reordered
    assert all(c.meta["section"] == "doc.pdf" for c in chunks)  # heading-less


def test_oversized_block_does_not_fragment_following_blocks() -> None:
    # One oversized prose block (split at sentences) must not balloon into a
    # single over-budget chunk; the blocks after it still chunk normally.
    giant = "".join(f"第{i}点内容说明。" for i in range(1, 20))
    blocks = [h(1, "财务报告"), p(giant), p("甲" * 30), p("乙" * 30)]
    # min_tokens=0: this test verifies the split, not the merge pass.
    chunks = chunk_doc("doc.pdf", blocks, chunk_size=60, min_tokens=0)
    # 不宜过大: no prose chunk exceeds the budget (whitespace block-joins are a
    # couple ASCII tokens at most, so content tokens are what matters).
    assert all(estimate_tokens("".join(c.text.split())) <= 60 for c in chunks)
    assert all(c.meta["section"] == "财务报告" for c in chunks)
    # nothing lost, duplicated, or reordered across the whole document
    joined = "".join(c.text for c in chunks).replace("\n\n", "")
    assert joined == giant + "甲" * 30 + "乙" * 30
    assert any("甲" in c.text for c in chunks) and any("乙" in c.text for c in chunks)


def test_oversized_run_without_sentence_breaks_hard_cuts() -> None:
    # No sentence punctuation at all → last-resort character cut, order kept.
    blob = "数" * 500
    blocks = [p(blob)]
    chunks = chunk_doc("doc.pdf", blocks, chunk_size=100)
    assert len(chunks) == 5
    assert "".join(c.text for c in chunks) == blob
    assert all(c.meta["section"] == "doc.pdf" for c in chunks)


# ---------------------------------------------------------- page furniture ----


def test_header_footer_go_to_meta_not_text() -> None:
    blocks = [hdr("郑州银行 2025年年度报告"), h(1, "财务报告"), p("正文内容。"), ftr("第 12 页")]
    chunks = chunk_doc("doc.pdf", blocks)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.meta["header"] == "郑州银行 2025年年度报告"
    assert c.meta["footer"] == "第 12 页"
    assert "郑州银行 2025年年度报告" not in c.text
    assert "第 12 页" not in c.text


# --------------------------------------------------------------------- meta --


def test_page_and_pages_meta() -> None:
    blocks = [h(1, "财务报告", page=45), p("正文一。", page=45), p("正文二。", page=46)]
    chunks = chunk_doc("doc.pdf", blocks)
    c = chunks[0]
    assert c.meta["page"] == 45
    assert c.meta["pages"] == "45-46"


def test_chunk_id_and_order() -> None:
    blocks = [h(1, "甲"), p("内容一"), h(1, "乙"), p("内容二")]
    chunks = chunk_doc("doc.pdf", blocks)
    assert [c.chunk_id for c in chunks] == ["doc.pdf#0", "doc.pdf#1"]
    assert [c.order for c in chunks] == [0, 1]
    assert all(c.doc_id == "doc.pdf" for c in chunks)


# ------------------------------------------------------------------- merging --


def test_min_tokens_merges_adjacent_same_section_chunks() -> None:
    blocks = [h(1, "财务报告"), p("甲" * 50), p("乙" * 50), h(1, "风险"), p("丙" * 50)]
    chunks = chunk_doc("doc.pdf", blocks, chunk_size=40, min_tokens=60)
    assert len(chunks) == 2
    assert "甲" in chunks[0].text and "乙" in chunks[0].text
    assert chunks[0].meta["section"] == "财务报告"
    assert chunks[1].meta["section"] == "风险"
    # the trailing "丙" paragraph did not merge across the section boundary


def test_min_tokens_zero_keeps_strict_section_boundaries() -> None:
    # Two 30-token paragraphs under a 40-token budget: strict boundary, no merge.
    blocks = [h(1, "财务报告"), p("甲" * 30), p("乙" * 30)]
    chunks = chunk_doc("doc.pdf", blocks, chunk_size=40, min_tokens=0)
    assert len(chunks) == 2


def test_oversized_sheet_attribution_folds_back_into_table_chunk() -> None:
    # One xlsx sheet = one chunk: a heading-less doc whose table alone blows the
    # budget owns its own chunk, and the trailing attribution paragraph ("数据来源：
    # Wind") folds back in under the default min_tokens instead of becoming an
    # orphan chunk #1. Both carry the doc id as the section anchor.
    huge = "| 项 | 值 |\n| --- | --- |\n" + "| 1 | 2 |\n" * 300  # > chunk_size
    blocks = [t(huge), p("数据来源：Wind")]
    chunks = chunk_doc("5年口径_天津银行[1578.HK]_-_资产负债表.xlsx", blocks, chunk_size=300)
    assert len(chunks) == 1
    c = chunks[0]
    assert "数据来源：Wind" in c.text
    assert c.meta["table_ids"] == ["T1"]
    assert c.meta["section"] == "5年口径_天津银行[1578.HK]_-_资产负债表.xlsx"


# --------------------------------------------------------------- estimator ----


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文十个字一二三四五") == 10  # CJK ≈ 1 token/char
    assert estimate_tokens("abcd") == 1  # ASCII ≈ 4 chars/token
    assert estimate_tokens("abcdefgh") == 2


# --------------------------------------------------------- tokenizer counter --


class _FakeTokenizer:
    """1 "token" per character — a stand-in for a real AutoTokenizer."""

    def encode(self, text: str) -> list[str]:
        return list(text)


def test_tokenizer_counter_uses_tokenizer_when_available(monkeypatch) -> None:
    monkeypatch.setattr(chunker, "_load_tokenizer", lambda model: _FakeTokenizer())
    counter = chunker.TokenizerCounter(model="fake/qwen3-embedding")
    assert counter("中文abc") == 5  # per-char encoding
    assert counter("") == 0


def test_tokenizer_counter_falls_back_to_heuristic(monkeypatch) -> None:
    monkeypatch.setattr(chunker, "_load_tokenizer", lambda model: None)
    counter = chunker.TokenizerCounter(model="no/such-model")
    assert counter("中文十个字一二三四五") == 10  # heuristic count
    assert counter("abcd") == 1
