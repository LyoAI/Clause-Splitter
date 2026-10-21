"""Tests for the HTML 复核展示 pipeline (compare.retrieve + compare.review).

The review page is the most important increment for 作业人员: every external
clause shows the deterministic top-k internal clauses that were recalled, so
the operator can judge relevance / trust against the episode's own result.
These tests pin (1) the cross-backend merge in ``pre_retrieve`` and (2) that
``render_review_html`` produces a self-contained page carrying candidates,
keyword highlights, and the 「✓ 采用」 adopted marker.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from clause_splitter.compare.retrieve import highlight, pre_retrieve
from clause_splitter.compare.review import _min_display_candidates, render_review_html
from clause_splitter.models import (
    CompareResult,
    CoverageStatus,
    MatchResult,
    RetrievedCandidate,
)
from chillrag.tools.searcher.base import BaseSearcher, Hit
from chillrag.tools.searcher.store import Chunk, DocumentStore


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
    assert 'class="col-score"' in html_text
    # 检索后端列已去掉(窄栏把宽度让给条款文本);后端只保留在导出 Excel 的召回行里。
    assert 'class="col-backend"' not in html_text
    assert 'class="backend-badge"' not in html_text
    assert "数据安全办法" in html_text
    assert "第二条" in html_text
    # verbatim keyword mark on the adopted candidate, substring-fallback mark on the other
    assert "<mark>应当加密存储</mark>" in html_text
    assert "<mark>数据</mark>" in html_text
    # exactly one candidate row is adopted (the un-adopted one isn't)
    assert html_text.count('class="cand-row adopted"') == 1
    assert html_text.count('<span class="adopted-mark">✓</span>') == 1
    assert "第一条" in html_text
    # 第四栏「采用内规」:被采用条款摘要 + 内容 snippet(截断)。
    assert 'class="match-snippet"' in html_text
    assert "第二条　重要数据应当加密存储,并实施访问控制。" in html_text
    # 弹窗查看全文:召回行/采用 snippet 带 data-full,页面含弹窗容器。
    assert 'id="modal"' in html_text and 'id="modal-text"' in html_text
    assert 'data-full="' in html_text
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
        clause_type="义务性条款", internalization_decision="需要内化",
        internalization_reason="义务性条款且涉及核心业务。",
        difference_analysis="外规要求加密存储并实施访问控制;内规仅覆盖加密存储,遗漏访问控制要求。",
        core_regulatory_requirements="1）、对重要数据加密存储\n2）、实施访问控制",
        remedy_suggestion="建议在数据安全办法中补充访问控制条款。",
        retrieved_candidates=candidates or [],
    )


def test_review_groups_candidates_by_document(tmp_path):
    """召回来自同一法律条款文件的片段聚合在一个 doc-group 下。

    采用候选不再单独进第四栏,与未采用候选一起在四栏之下的召回区按来源制度
    分组:数据安全办法(2 条) + 财务报销制度(1 条) → 共 2 个 doc-group。
    """
    match = _partial_match(candidates=[
        _cand("数据安全办法#1", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。", adopted=True),
        _cand("数据安全办法#0", "数据安全办法", "第一条", "第一条　本制度适用于数据分类分级。"),
        _cand("财务报销#3", "财务报销制度", "第四条", "第四条　差旅报销需附发票。"),
    ])
    html_text = _render(tmp_path, match)
    assert html_text.count('class="doc-group"') == 2
    assert "📄 数据安全办法" in html_text
    assert "📄 财务报销制度" in html_text
    assert "第四条　差旅报销需附发票。" in html_text


def _strip_marks(html_text: str) -> str:
    """去掉 <mark> 高亮标签,还原片段原始文本(用于精确字符串断言)。"""
    return re.sub(r"</?mark>", "", html_text)


def test_review_dedupes_identical_full_text(tmp_path):
    """相同全文的重复候选(分块边界/跨后端)在召回表里只展示一次。"""
    match = _partial_match(candidates=[
        _cand("数据安全办法#1", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。", adopted=True),
        _cand("数据安全办法#1b", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。"),
        _cand("数据安全办法#0", "数据安全办法", "第一条", "第一条　本制度适用于数据分类分级。"),
    ])
    html_text = _render(tmp_path, match)
    # 去重作用域是召回表的可见单元格(第四栏 snippet 与弹窗 data-full 属性刻意
    # 含被采用条款文本,不计入):同一全文只渲染一个可见行。
    recall = html_text[html_text.index("检索召回 top-k"):]
    assert _strip_marks(recall).count(">第二条　重要数据应当加密存储。</td>") == 1


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
    # 长全文不渲染为可见文本,只进弹窗的 data-full 属性;表格单元格仅 snippet。
    assert html_text.count(long_text) == 1
    assert ('data-full="' + long_text) in html_text
    assert ('<td class="col-text">' + long_text + "</td>") not in html_text
    assert "<mark>" in html_text
    assert "重要数据应当加密存储" in _strip_marks(html_text)
    # 卡片窗口不做「展开全文」(与窗口内滚动冗余,已移除),全文走弹窗。
    assert 'class="card-expand-btn"' not in html_text
    assert "展开全文" not in html_text


def test_review_modal_carries_full_text(tmp_path):
    """召回行与第四栏采用 snippet 都携带 data-full(点击弹窗展示完整条款)。"""
    match = _partial_match(candidates=[
        _cand("数据安全办法#1", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。", adopted=True),
    ])
    html_text = _render(tmp_path, match)
    # 召回行:行级 data-full = 候选全文,data-title = 制度名 + 条款号。
    assert 'data-full="第二条　重要数据应当加密存储。"' in html_text
    assert 'data-title="数据安全办法 第二条"' in html_text
    # 第四栏采用 snippet 同样携带全文(弹窗内容不截断)。
    assert html_text.count('class="match-snippet"') == 1
    snippet = html_text[html_text.index('class="match-snippet"'):]
    assert 'data-full="第二条　重要数据应当加密存储。"' in snippet
    # 弹窗容器 + 关闭按钮在页面里(JS 控制,静态断言只查结构)。
    assert 'id="modal"' in html_text and 'id="modal-text"' in html_text
    assert 'class="modal-close"' in html_text


def test_review_embeds_export_data(tmp_path):
    """导出 Excel:控制栏有按钮,页面内嵌 export-data JSON(原始文本、采用 ✓ 前缀)。"""
    match = _partial_match(candidates=[
        _cand("数据安全办法#1", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。", adopted=True),
        _cand("数据安全办法#0", "数据安全办法", "第一条", "第一条　本制度适用于数据分类分级。"),
    ])
    html_text = _render(tmp_path, match)
    assert 'id="export-excel"' in html_text and "📊 导出 Excel" in html_text
    tag = '<script id="export-data" type="application/json">'
    start = html_text.index(tag) + len(tag)
    data = json.loads(html_text[start:html_text.index("</script>", start)])
    assert data["name"].startswith("内规检索复核-")
    row = data["rows"][0]
    assert row["clause"] == "第一条" and row["status"] == "部分覆盖"
    assert row["external"] == "外规要求重要数据加密存储。"
    assert "对重要数据加密存储" in row["core"]              # 核心监管要求
    assert "义务性条款且涉及核心业务。" in row["intl_reason"]  # 内化判定依据
    assert "建议在数据安全办法中补充访问控制条款。" in row["remedy"]  # 整改建议
    assert "【数据安全办法 第二条】" in row["adopted"]       # 采用内规 + snippet
    assert "✓ 数据安全办法 第二条" in row["recall"]          # 采用候选 ✓ 前缀、优先
    assert "（bm25 1.0000）" in row["recall"]                # 页面去掉的后端列,导出仍保留
    assert "第一条　本制度适用于数据分类分级。" in row["recall"]  # 未采用候选全文
    assert "外规要求加密存储并实施访问控制" in row["diff"]    # 差异说明(部分覆盖)


def test_review_renders_difference_and_internalization_badge(tmp_path):
    """部分覆盖在「差异说明」栏(合并栏)渲染判定分析 + 逐条差异;内化判定依据
    在「核心监管要求」栏内独立子块展示。"""
    html_text = _render(tmp_path, _partial_match())
    assert "🆚 差异说明" in html_text
    assert "外规要求加密存储并实施访问控制" in html_text
    # 卡片头「需要内化」徽标;内化判定依据放进第一栏子块(不再只进报告)。
    assert '<span class="intl-badge intl-must">需要内化</span>' in html_text
    assert "🧭 内化判定依据" in html_text
    assert "义务性条款且涉及核心业务。" in html_text


def test_review_internalization_pending_when_empty(tmp_path):
    """字段为空(旧数据) → 卡片头灰色「待判定」徽标。"""
    match = _partial_match()
    match.clause_type = ""
    match.internalization_decision = ""
    match.internalization_reason = ""
    html_text = _render(tmp_path, match)
    assert '<span class="intl-badge intl-pending">待判定</span>' in html_text


def test_review_difference_panel_state_by_status(tmp_path):
    """差异说明栏(合并栏)四栏恒定:仅部分覆盖有逐条内容,其余状态给占位文案。"""
    partial = _render(tmp_path, _partial_match())
    assert "外规要求加密存储并实施访问控制" in partial  # 部分覆盖 → 逐条内容
    full = _partial_match()
    full.status = CoverageStatus.FULLY_COVERED
    full_html = _render(tmp_path, full)
    assert "🆚 差异说明" in full_html  # 栏恒在
    assert "外规要求加密存储并实施访问控制" not in full_html  # 内容不残留
    assert "无内外规差异" in full_html  # 占位表达「已检查、无差异」
    missing = _partial_match()
    missing.status = CoverageStatus.NOT_COVERED
    missing.matched_internal_id = None
    missing_html = _render(tmp_path, missing)
    assert "缺失，无内规可对比" in missing_html


def test_review_core_requirements_between_source_and_analysis(tmp_path):
    """卡片体内:外规原文 → 核心监管要求 → 差异说明(判定分析小节)→ 整改建议。"""
    html_text = _render(tmp_path, _partial_match())
    # 新段落标题 + 编号分点逐行展示。
    assert "📋 核心监管要求" in html_text
    assert '1）、对重要数据加密存储</div>' in html_text
    assert '2）、实施访问控制</div>' in html_text
    # 顺序:外规原文 → 核心监管要求 → 差异说明(合并栏)→ 整改建议。
    assert (
        html_text.index("外规原文")
        < html_text.index("核心监管要求")
        < html_text.index("差异说明")
        < html_text.index("整改建议")
    )
    # 内化判定依据放进第一栏子块,在核心监管要求内容之后。
    assert "内化判定依据" in html_text
    # 整改建议恢复展示(合并栏之后)。
    assert "🛠️ 整改建议" in html_text
    assert "建议在数据安全办法中补充访问控制条款。" in html_text


def test_review_four_column_panels_layout(tmp_path):
    """卡片体内横向四栏:外规原文(原位顶部)之后,核心监管要求(含内化判定依据
    子块)/差异说明(合并判定分析+内外规不一致)/整改建议/采用内规&检索召回
    四栏并排;检索召回 top-k 收进第四栏,栏体固定高度窗口滚动(不再有四栏之下
    的置底召回区)。"""
    html_text = _render(tmp_path, _partial_match(candidates=[
        _cand("数据安全办法#1", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。", adopted=True),
        _cand("数据安全办法#0", "数据安全办法", "第一条", "第一条　本制度适用于数据分类分级。"),
    ]))
    # 外规原文保持原位(顶部 details),在四栏容器之前。
    assert html_text.index("外规原文") < html_text.index('class="panels"')
    # 四栏恒在,且 DOM 顺序:核心 → 差异说明 → 整改建议 → 采用内规&检索召回。
    assert html_text.count('<div class="panel ') == 4
    order = [
        html_text.index("核心监管要求"),
        html_text.index("差异说明"),
        html_text.index("整改建议"),
        html_text.index("采用内规"),
    ]
    assert order == sorted(order)
    assert "✅ 采用内规&检索召回" in html_text
    # 检索召回收进第四栏:召回小节在 panel-inner 内部,采用摘要在其前。
    inner = html_text[html_text.index("panel panel-inner"):]
    assert inner.index('class="match"') < inner.index("检索召回 top-k")
    assert 'class="doc-group"' not in inner[:inner.index("检索召回 top-k")]
    # 四栏之下的置底召回区不复存在;第四栏栏体为固定高度滚动窗口。
    assert "recall-under" not in html_text
    assert 'max-height:var(--panel-window' in html_text
    # 召回小节:头部带全部候选条数(采用+未采用);本卡 2 条 ≤ 每制度默认 2 条 → 全展示、无「展开更多」。
    assert 'class="recall-head"' in html_text
    assert "检索召回 top-k" in html_text
    assert "(2 条)</span>" in html_text
    assert 'class="recall-body truncated"' not in html_text  # 未触发折叠
    assert 'class="recall-more-btn"' not in html_text  # 无折叠候选 → 无按钮
    # 无全局全部展开/收起按钮。
    assert "expand-all" not in html_text
    assert "collapse-all" not in html_text


def test_review_recall_truncated_with_expand_more(tmp_path):
    """检索召回区:每个来源制度默认只展示前 2 条,超出的行折叠 +「展开更多」按钮。"""
    match = _partial_match(candidates=[
        _cand("a#0", "数据安全办法", "第一条", "第一条　一", score=1.0),
        _cand("a#1", "数据安全办法", "第二条", "第二条　二", score=2.0),
        _cand("a#2", "数据安全办法", "第三条", "第三条　三", score=3.0),
        _cand("a#3", "数据安全办法", "第四条", "第四条　四", score=4.0),
        _cand("a#4", "数据安全办法", "第五条", "第五条　五", score=5.0),
    ])
    html_text = _render(tmp_path, match)
    # 折叠态:recall-body 带 truncated,5 行仍在 DOM(第 3 行起由 CSS 隐藏)。
    assert 'class="recall-body truncated"' in html_text
    assert html_text.count('class="cand-row"') == 5
    # 「展开更多」按钮:带剩余条数 + 展开/收起切换文案。
    assert "展开更多 (3 条) ▾" in html_text
    assert 'data-less="收起 ▴"' in html_text
    # 组头条数仍显示该制度全部候选数,便于知道折叠了多少。
    assert "5 条</span>" in html_text


def test_review_core_panel_placeholder_when_empty(tmp_path):
    """核心监管要求为空(定义/目的/附则类条款或旧数据)→ 四栏仍恒定,
    栏内给占位文案,不渲染任何分点内容。"""
    match = _partial_match()
    match.core_regulatory_requirements = ""
    html_text = _render(tmp_path, match)
    assert "核心监管要求" in html_text  # 栏头恒在
    assert 'data-ann-id="m0:core:' not in html_text  # 无分点 → 无可编辑行
    assert "无具体合规义务" in html_text  # 占位
    assert "判定分析" in html_text


def test_review_multiline_analysis_and_difference(tmp_path):
    """判定分析/差异说明含换行分点 → 每点独占一行;旧数据单行含编号标记也能拆行。"""
    match = _partial_match()
    match.analysis = "1）、内规已承接加密存储\n2）、但遗漏访问控制"
    match.difference_analysis = "1）、外规要求加密存储\n2）、内规未提访问控制"
    html_text = _render(tmp_path, match)
    assert '1）、内规已承接加密存储</div>' in html_text
    assert '2）、但遗漏访问控制</div>' in html_text
    assert '1）、外规要求加密存储</div>' in html_text
    assert '2）、内规未提访问控制</div>' in html_text
    # 旧数据:无换行但有编号标记 → 同样拆行(标记保留在原项)。
    match2 = _partial_match()
    match2.analysis = "1）、建立分类分级制度2）、实施安全管理"
    html_text2 = _render(tmp_path, match2)
    assert '1）、建立分类分级制度</div>' in html_text2
    assert '2）、实施安全管理</div>' in html_text2


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
    assert '<div class="stat s-miss"><b>0</b><span>缺失</span></div>' in html_text
    assert '<div class="stat s-none"><b>1</b><span>无需内化</span></div>' in html_text


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
    assert '<div class="stat s-miss"><b>1</b><span>缺失</span></div>' in html_text
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
    """同制度组内采用行优先(即使得分更低);得分独立一列(后端列已去掉)。"""
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


def test_review_editable_and_annotation_hooks(tmp_path):
    """批注/编辑(选中文字触发):可批注单元带稳定 data-ann-id + 原文
    data-original;选中后浮出工具条(✍️ 编辑/💬 批注)→ 编辑弹窗(引用 +
    修改输入)/ 批注弹窗(引用 + 署名默认匿名 + 内容);便签贴卡片底部。"""
    match = _partial_match(candidates=[
        _cand("数据安全办法#1", "数据安全办法", "第二条", "第二条　重要数据应当加密存储。", adopted=True),
    ])
    html_text = _render(tmp_path, match)
    # 可批注单元:外规原文 + 三栏正文逐行,均带稳定定位 ID 与原文(供导出对照)。
    assert 'data-ann-id="m0:external"' in html_text
    assert 'data-ann-id="m0:core:0"' in html_text and 'data-ann-id="m0:core:1"' in html_text
    assert 'data-ann-id="m0:intl:0"' in html_text      # 内化判定依据子块可编辑
    assert 'data-ann-id="m0:analysis:0"' in html_text  # 合并栏:判定分析小节
    assert 'data-ann-id="m0:diff:0"' in html_text      # 合并栏:内外规不一致小节
    assert 'data-ann-id="m0:remedy:0"' in html_text    # 整改建议可编辑
    assert 'data-original="1）、对重要数据加密存储"' in html_text
    # 选中文字 → 浮出工具条。
    assert 'id="selbar"' in html_text
    assert "✍️ 编辑" in html_text and "💬 批注" in html_text
    # 编辑弹窗:引用选中文字 + 修改输入;进出动效 10ms 近乎即时。
    assert 'transition-duration:.01s' in html_text
    assert 'modal-backdrop fast' in html_text
    assert 'id="edit-modal"' in html_text and 'id="edit-quote"' in html_text
    assert "修改后的文字" in html_text and "请输入修改后的文字" in html_text
    # 批注弹窗:引用 + 署名(默认匿名)+ 批注内容。
    assert 'id="comment-modal"' in html_text and 'id="comment-quote"' in html_text
    assert 'id="comment-author"' in html_text and "默认匿名" in html_text
    assert "请输入批注内容" in html_text
    # 批注便签容器(卡片底部)、文中黄条引用标记、点标记弹出的内容气泡。
    assert 'data-notes-for="m0"' in html_text
    assert "quote-mark" in html_text
    assert 'id="note-pop"' in html_text and 'class="pop-item"' not in html_text  # 气泡条目由 JS 动态生成
    # 控制栏:导出/导入批注按钮 + 隐藏文件选择(只收 JSON)。
    assert 'id="export-notes"' in html_text and "📝 导出批注" in html_text
    assert 'id="import-notes"' in html_text and "📥 导入批注" in html_text
    assert 'id="import-file" accept=".json' in html_text
    # 批注引擎关键行为:按选区定位单元、localStorage 暂存、导入恢复。
    assert "getSelection" in html_text and "localStorage" in html_text
    assert "applyAnnotations" in html_text
    # 卡片头不再有 💬 批注按钮(批注一律由选中文字触发)。
    assert "card-note-btn" not in html_text
    assert "contenteditable" not in html_text


def test_review_intro_modal_grid(tmp_path):
    """使用说明弹窗:宫格功能卡(非长列表),「我知道了」绿色框按钮,
    5 秒自动关闭;头部带自动关闭提示。"""
    html_text = _render(tmp_path, _partial_match())
    assert 'id="intro"' in html_text
    assert 'class="intro-grid"' in html_text
    assert html_text.count('class="intro-item"') == 6
    assert 'class="intro-ok"' in html_text and "我知道了" in html_text
    assert "⏳ 5 秒后自动关闭" in html_text
    assert "setTimeout(closeIntro, 5000)" in html_text
    assert "setInterval" in html_text  # 倒计时 5→1
    # 导出功能都在说明卡里介绍到。
    assert "导出 Excel" in html_text and "批注 JSON" in html_text
