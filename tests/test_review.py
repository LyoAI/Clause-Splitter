"""Tests for the HTML 复核展示 pipeline (compare.retrieve + compare.review).

The review page is the most important increment for 作业人员: every external
clause shows the deterministic top-k internal clauses that were recalled, so
the operator can judge relevance / trust against the episode's own result.
These tests pin (1) the cross-backend merge in ``pre_retrieve`` and (2) that
``render_review_html`` produces a self-contained page carrying candidates,
keyword highlights, and the 「✓ 采用」 adopted marker.
"""

from __future__ import annotations

import re
from pathlib import Path

from clause_splitter.compare.retrieve import highlight, pre_retrieve
from clause_splitter.compare.review import _min_display_candidates, render_review_html
from clause_splitter.models.compare import (
    CompareResult,
    CoverageStatus,
    MatchResult,
    RetrievedCandidate,
)
from clause_splitter.tools.searcher.base import BaseSearcher, Hit
from clause_splitter.tools.searcher.store import Chunk, DocumentStore


class FakeSearcher(BaseSearcher):
    """Deterministic searcher: returns the hits it was constructed with."""

    name = "fake"

    def __init__(self, name: str, hits: list[Hit]) -> None:
        self.name = name
        self._hits = hits

    def search(self, query: str, k: int) -> list[Hit]:
        return self._hits[:k]


def _store() -> DocumentStore:
    store = DocumentStore(name="review-test")
    store.add_chunk(Chunk(
        chunk_id="数据安全办法#0", doc_id="数据安全办法", order=0,
        text="第一条　本制度适用于数据分类分级与数据安全风险评估。",
        meta={"section": "第一章　总则 > 第一条"},
    ))
    store.add_chunk(Chunk(
        chunk_id="数据安全办法#1", doc_id="数据安全办法", order=1,
        text="第二条　重要数据应当加密存储,并实施访问控制。",
        meta={"section": "第一章　总则 > 第二条"},
    ))
    return store


def test_pre_retrieve_merges_backends_by_chunk_id() -> None:
    store = _store()
    searchers = [
        FakeSearcher("grep", [
            Hit(score=5.0, chunk_id="数据安全办法#1", text="第二条　重要数据应当加密存储。", meta={}),
            Hit(score=3.0, chunk_id="数据安全办法#0", text="第一条　本制度适用于数据分类分级。", meta={}),
        ]),
        FakeSearcher("bm25", [
            Hit(score=4.0, chunk_id="数据安全办法#1", text="第二条　重要数据应当加密存储。", meta={}),
        ]),
    ]
    cands = pre_retrieve(store, searchers, "数据应当加密存储", top_k=5)
    assert [c.chunk_id for c in cands] == ["数据安全办法#1", "数据安全办法#0"]
    top = cands[0]
    # best score wins across backends; backends joined deterministically
    assert top.score == 5.0
    assert top.backend == "grep,bm25"
    # provenance resolved from the store (single source of truth)
    assert top.doc_name == "数据安全办法"
    assert top.clause_label == "第二条"
    assert "加密存储" in top.full_text


def test_pre_retrieve_empty_without_searchers() -> None:
    assert pre_retrieve(_store(), [], "任意条款") == []


def test_highlight_escapes_and_merges_spans() -> None:
    out = highlight("数据<分类>分级", "数据 分类")
    # user text escaped; both keyword spans wrapped, overlapping spans merged
    assert out == "<mark>数据</mark>&lt;<mark>分类</mark>&gt;分级"


def test_highlight_verbatim_keyword_run() -> None:
    # punctuation-split keyword appears verbatim → single mark around the run
    out = highlight("第二条　重要数据应当加密存储,并实施访问控制。", "外规要求重要数据，应当加密存储。")
    assert "<mark>应当加密存储</mark>" in out
    assert "<mark>重要数据</mark>" not in out  # run not contiguous, not over-marked


def test_highlight_falls_back_to_shared_substrings() -> None:
    # a long unpunctuated clause run shares only a fragment with the snippet;
    # the shared fragment is highlighted (overlapping spans merged into one mark)
    out = highlight("本条要求建立健全数据安全管理制度。", "金融机构应当建立健全数据安全管理制度")
    assert "<mark>建立健全数据安全管理制度</mark>" in out
    # unrelated text stays outside the mark
    assert out.startswith("本条要求")


def test_render_review_html_self_contained(tmp_path: Path) -> None:
    match = MatchResult(
        external_clause_id="0",
        external_document_name="外部管理办法",
        external_article_id="第一条",
        external_source_text="外规要求重要数据加密存储。",
        status=CoverageStatus.FULLY_COVERED,
        matched_internal_id="数据安全办法#1",
        matched_internal_document_name="数据安全办法",
        matched_internal_clause_number="第二条",
        matched_internal_source_text="第二条　重要数据应当加密存储。",
        analysis="内规第二条已明确覆盖加密存储要求。",
        remedy_suggestion="",
        retrieved_candidates=[
            RetrievedCandidate(
                chunk_id="数据安全办法#1",
                doc_name="数据安全办法",
                clause_label="第二条",
                backend="bm25",
                score=4.0,
                query="外规要求重要数据，应当加密存储。",
                snippet="第二条　重要数据应当加密存储,并实施访问控制。",
                full_text="第二条　重要数据应当加密存储,并实施访问控制。",
                adopted=True,
            ),
            RetrievedCandidate(
                chunk_id="数据安全办法#0",
                doc_name="数据安全办法",
                clause_label="第一条",
                backend="grep",
                score=1.0,
                query="外规要求重要数据，应当加密存储。",
                snippet="第一条　本制度适用于数据分类分级。",
                full_text="第一条　本制度适用于数据分类分级。",
                adopted=False,
            ),
        ],
    )
    result = CompareResult(
        external_documents=["外部管理办法"],
        internal_documents=["数据安全办法"],
        total_external_obligations=1,
        total_internal_obligations=2,
        matches=[match],
    )
    out = tmp_path / "内规检索复核.html"
    render_review_html(result, out, store_name="internal")

    html_text = out.read_text(encoding="utf-8")
    assert out.exists()
    # self-contained page markers
    assert "<!doctype html>" in html_text
    assert "⚖️ 内规检索复核" in html_text
    assert "内规库" in html_text and "internal" in html_text
    # card: status badge + external text + analysis
    assert "完全覆盖" in html_text
    assert "外规要求重要数据加密存储。" in html_text
    assert "内规第二条已明确覆盖加密存储要求。" in html_text
    # recall list: grouped by source doc, table rows, backend badge, keyword <mark>, adopted marker
    assert 'class="doc-group"' in html_text
    assert 'class="cand-table"' in html_text
    assert 'class="col-no"' in html_text and 'class="col-text"' in html_text
    assert 'class="col-backend"' in html_text and 'class="col-score"' in html_text
    assert "数据安全办法" in html_text
    assert "第二条" in html_text
    assert "bm25" in html_text
    # verbatim keyword mark on the adopted candidate, substring-fallback mark on the other
    assert "<mark>应当加密存储</mark>" in html_text
    assert "<mark>数据</mark>" in html_text
    # exactly one candidate row is adopted (the un-adopted one isn't)
    assert html_text.count('class="cand-row adopted"') == 1
    assert html_text.count('<span class="adopted-mark">✓</span>') == 1
    assert "第一条" in html_text
    # adopted candidate carries the full text; no separate 承接内规原文 block (dup removed)
    assert "承接内规原文" not in html_text
    # internalization decision not set on this match → gray 待判定 badge
    assert "待判定" in html_text
    # card head is a collapsible toggle
    assert 'class="card-head" role="button"' in html_text
    assert 'class="card-body"' in html_text
    # coverage stats
    assert "覆盖率" in html_text


def _cand(chunk_id: str, doc: str, label: str, text: str, *, adopted: bool = False, score: float = 1.0) -> RetrievedCandidate:
    return RetrievedCandidate(
        chunk_id=chunk_id, doc_name=doc, clause_label=label, backend="bm25",
        score=score, query="外规要求数据加密", snippet=text, full_text=text, adopted=adopted,
    )


def _render(tmp_path, match: MatchResult) -> str:
    result = CompareResult(
        external_documents=["外部管理办法"], internal_documents=["数据安全办法"],
        total_external_obligations=1, total_internal_obligations=2, matches=[match],
    )
    out = tmp_path / "复核.html"
    render_review_html(result, out, store_name="internal")
    return out.read_text(encoding="utf-8")


def _partial_match(candidates=None) -> MatchResult:
    return MatchResult(
        external_clause_id="0", external_document_name="外部管理办法", external_article_id="第一条",
        external_source_text="外规要求重要数据加密存储。",
        status=CoverageStatus.PARTIALLY_COVERED,
        matched_internal_id="数据安全办法#1", matched_internal_document_name="数据安全办法",
        matched_internal_clause_number="第二条",
        matched_internal_source_text="第二条　重要数据应当加密存储。",
        analysis="内规仅覆盖加密,遗漏访问控制。",
        remedy_suggestion="修订第二条:补充访问控制要求。",
        clause_type="义务性条款", internalization_decision="需要内化",
        internalization_reason="义务性条款且涉及核心业务。",
        difference_analysis="外规要求加密存储并实施访问控制;内规仅覆盖加密存储,遗漏访问控制要求。",
        retrieved_candidates=candidates or [],
    )


def test_review_groups_candidates_by_document(tmp_path):
    """召回来自同一法律条款文件的片段聚合在一个 doc-group 下。"""
    match = _partial_match(candidates=[
        _cand("数据安全办法#1", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。", adopted=True),
        _cand("数据安全办法#0", "数据安全办法", "第一条", "第一条　本制度适用于数据分类分级。"),
        _cand("财务报销#3", "财务报销制度", "第四条", "第四条　差旅报销需附发票。"),
    ])
    html_text = _render(tmp_path, match)
    # 两个来源制度 → 两个 doc-group 头;同制度候选聚合在同一个组里
    assert html_text.count('class="doc-group"') == 2
    assert "📄 数据安全办法" in html_text
    assert "📄 财务报销制度" in html_text
    assert "第四条　差旅报销需附发票。" in html_text


def _strip_marks(html_text: str) -> str:
    """去掉 <mark> 高亮标签,还原片段原始文本(用于精确字符串断言)。"""
    return re.sub(r"</?mark>", "", html_text)


def test_review_dedupes_identical_full_text(tmp_path):
    """相同全文的重复候选(分块边界/跨后端)只展示一次。"""
    match = _partial_match(candidates=[
        _cand("数据安全办法#1", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。", adopted=True),
        _cand("数据安全办法#1b", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。"),
        _cand("数据安全办法#0", "数据安全办法", "第一条", "第一条　本制度适用于数据分类分级。"),
    ])
    html_text = _render(tmp_path, match)
    assert _strip_marks(html_text).count("第二条　重要数据应当加密存储。") == 1


def test_review_candidate_shows_snippet_only(tmp_path):
    """召回候选只展示关键词高亮的 snippet,不再渲染「展开全文」(条款普遍短)。"""
    long_text = "第二条　" + "重要数据应当加密存储并实施访问控制。" * 20  # 远超 180 字窗口
    match = _partial_match(candidates=[
        _cand("数据安全办法#1", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。", adopted=True),
        RetrievedCandidate(
            chunk_id="数据安全办法#1b", doc_name="数据安全办法", clause_label="第二条",
            backend="bm25", score=1.0, query="外规要求数据加密",
            snippet="第二条　重要数据应当加密存储。", full_text=long_text,
        ),
    ])
    html_text = _render(tmp_path, match)
    # 任何候选都不渲染「展开全文」;snippet(带 <mark> 高亮)仍完整展示。
    assert "展开全文" not in html_text
    assert "<mark>" in html_text
    assert "重要数据应当加密存储" in _strip_marks(html_text)


def test_review_renders_difference_and_internalization_badge(tmp_path):
    """部分覆盖渲染差异说明;内化判定只以卡片头徽标呈现,无正文小节。"""
    html_text = _render(tmp_path, _partial_match())
    assert "差异说明(内外规不一致处)" in html_text
    assert "外规要求加密存储并实施访问控制" in html_text
    # 结果 = 卡片头的「需要内化」徽标;条款性质/依据不展示在页面。
    assert '<span class="intl-badge intl-must">需要内化</span>' in html_text
    assert "🎯 内化判定" not in html_text
    assert "条款性质" not in html_text
    assert "判定依据" not in html_text
    assert "义务性条款且涉及核心业务。" not in html_text


def test_review_internalization_pending_when_empty(tmp_path):
    """字段为空(旧数据) → 卡片头灰色「待判定」徽标。"""
    match = _partial_match()
    match.clause_type = ""
    match.internalization_decision = ""
    match.internalization_reason = ""
    html_text = _render(tmp_path, match)
    assert '<span class="intl-badge intl-pending">待判定</span>' in html_text


def test_review_difference_only_for_partial(tmp_path):
    """完全覆盖/缺失不渲染差异说明,即使字段有残留。"""
    match = _partial_match()
    match.status = CoverageStatus.FULLY_COVERED
    html_text = _render(tmp_path, match)
    assert "差异说明(内外规不一致处)" not in html_text


def _no_intl_match() -> MatchResult:
    """判定「无需内化」的外规条款(宣示性,不进入覆盖比对)。"""
    return MatchResult(
        external_clause_id="4", external_document_name="外部管理办法", external_article_id="第五条",
        external_source_text="本办法自2026年1月1日起施行,由监管机构负责解释。",
        status=CoverageStatus.NOT_COVERED,
        analysis="宣示性条款,无需承接。",
        remedy_suggestion="",
        clause_type="宣示性条款", internalization_decision="无需内化",
        internalization_reason="宣示性/程序性条款(生效日期与解释机关),无需内化为公司制度。",
    )


def _render_many(tmp_path, matches) -> str:
    result = CompareResult(
        external_documents=["外部管理办法"], internal_documents=["数据安全办法"],
        total_external_obligations=len(matches), total_internal_obligations=2, matches=matches,
    )
    out = tmp_path / "复核.html"
    render_review_html(result, out, store_name="internal")
    return out.read_text(encoding="utf-8")


def test_review_no_internalization_grouped_and_collapsed(tmp_path):
    """无需内化条款不进卡片流,集中折叠展示;统计里不当作缺口。"""
    html_text = _render_many(tmp_path, [_no_intl_match()])
    # 无任何需内化卡片;无需内化集中 section(默认折叠的 details)展示该条。
    assert 'class="card"' not in html_text
    assert 'class="no-intl"' in html_text
    assert "无需内化条款 (1 条)" in html_text
    assert "本办法自2026年1月1日起施行" in html_text
    # 统计:缺失 = 需内化缺失(0),无需内化 = 1。
    assert '<div class="stat"><b>0</b><span>缺失</span></div>' in html_text
    assert '<div class="stat"><b>1</b><span>无需内化</span></div>' in html_text


def test_review_mixes_cards_and_no_intl_section(tmp_path):
    """需要内化条款走卡片流;无需内化条款只在集中 section,缺失统计只算需内化。"""
    gap = _partial_match()  # 需要内化,缺失
    gap.external_article_id = "第三条"
    gap.status = CoverageStatus.NOT_COVERED
    gap.internalization_decision = "需要内化"
    gap.matched_internal_id = None
    html_text = _render_many(tmp_path, [_no_intl_match(), gap])
    assert html_text.count('<section class="card"') == 1
    assert 'class="no-intl"' in html_text
    # 缺失统计只算需内化处理条款(1),不含无需内化那条(2)。
    assert '<div class="stat"><b>1</b><span>缺失</span></div>' in html_text
    # 无需内化条目(第五条)出现在集中 section,不在卡片区。
    no_intl_block = html_text[html_text.index('class="no-intl"'):]
    assert "第五条" in no_intl_block
    assert "第五条" not in html_text[:html_text.index('class="no-intl"')]


def test_review_preventive_internalization_normalized_to_need(tmp_path):
    """旧数据「预防性内化」→ 渲染层归并为「需要内化」徽标,不再出现三值。"""
    match = _partial_match()
    match.internalization_decision = "预防性内化"
    html_text = _render(tmp_path, match)
    assert '<span class="intl-badge intl-must">需要内化</span>' in html_text
    assert "预防性内化" not in html_text


def test_review_candidate_table_adopted_first_within_group(tmp_path):
    """同制度组内采用行优先(即使得分更低);后端/得分各占一列。"""
    match = _partial_match(candidates=[
        _cand("数据安全办法#2", "数据安全办法", "第三条", "第三条　公司每年至少开展一次数据安全风险评估。", score=5.0),
        _cand("数据安全办法#1", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。", adopted=True, score=2.0),
    ])
    html_text = _render(tmp_path, match)
    table = html_text[html_text.index('<table class="cand-table">'):]
    assert table.index("第二条") < table.index("第三条")  # 采用行优先
    assert ">5.0000<" in html_text and ">2.0000<" in html_text  # 得分独立列


def test_min_display_candidates_topup_by_score():
    """最少保留 3 条:过滤后不足时按得分降序补齐,已保留的在前且不重复。"""
    cands = [
        _cand("a#0", "A", "第一条", "一", score=1.0),
        _cand("a#1", "A", "第二条", "二", score=9.0),
        _cand("a#2", "A", "第三条", "三", score=8.0),
        _cand("a#3", "A", "第四条", "四", score=7.0),
    ]
    # 严格过滤只留了 1 条 → 按分数补齐 2 条(9.0、8.0)。
    kept = _min_display_candidates([cands[0]], cands)
    assert [c.chunk_id for c in kept] == ["a#0", "a#1", "a#2"]
    # 已 ≥ 3 条 → 原样返回,不重复不追加。
    assert [c.chunk_id for c in _min_display_candidates(cands[:3], cands)] == ["a#0", "a#1", "a#2"]


def test_review_no_intl_row_vertical_full_reason(tmp_path):
    """无需内化行上下布局:上一行条款+原文,下一行判定依据完整不截断。"""
    match = _no_intl_match()
    long_reason = "宣示性/程序性条款(生效日期与解释机关)，" * 10  # 超长原因
    match.internalization_reason = long_reason
    html_text = _render_many(tmp_path, [match])
    row = html_text[html_text.index('class="no-intl-row"'):]
    assert 'class="row-main"' in row
    assert 'class="row-reason"' in row
    assert row.index("row-main") < row.index("row-reason")  # 上下,非左右
    # 原因完整展示:完整字符串出现,未被省略号/截断。
    assert long_reason in html_text
    assert "判定依据: " + long_reason in html_text
