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
from clause_splitter.compare.review import render_review_html
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
    # recall list: doc badge, clause label, backend badge, keyword <mark>, adopted marker
    assert "数据安全办法" in html_text
    assert "第二条" in html_text
    assert "bm25" in html_text
    # verbatim keyword mark on the adopted candidate, substring-fallback mark on the other
    assert "<mark>应当加密存储</mark>" in html_text
    assert "<mark>数据</mark>" in html_text
    # exactly one candidate carries the adopted badge (the un-adopted one doesn't)
    assert html_text.count('<span class="adopted">✓ 采用</span>') == 1
    assert "第一条" in html_text
    # coverage stats
    assert "覆盖率" in html_text
