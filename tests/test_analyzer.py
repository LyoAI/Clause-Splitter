"""analyze() short-circuits: the 内规/附件 path needs no Zeus and fills identity only."""

from __future__ import annotations

import asyncio

from clause_analysis import ClauseAnalyzer, ClauseInput
from rag.providers.zeus import ZeusProvider


def test_skip_internalization_needs_no_zeus_and_fills_identity_only():
    analyzer = ClauseAnalyzer(object(), ZeusProvider())  # unconfigured Zeus + unused llm
    clause = ClauseInput(
        term_name="第一条", term_id="c-1", term_content="网络运营者应当履行数据安全保护义务。"
    )
    res = asyncio.run(analyzer.analyze(clause, need_internalization=False, keyword_mode="rules"))

    assert res.term_id == "c-1"
    assert res.term_content == clause.term_content
    assert res.term_keyword  # rules extraction produced something
    assert res.internalize == ""
    assert res.coverage_status is None
    assert res.related_terms == []

    esg = res.to_esg()
    assert esg["termId"] == "c-1"
    assert esg["termKeyword"] == res.term_keyword
    assert esg["internalize"] == ""
    assert esg["coverageStatus"] == ""
    assert esg["relationTerms"] == []
