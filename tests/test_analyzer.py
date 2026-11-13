"""analyze() short-circuits: the 内规/附件 path needs no Zeus and fills identity only."""

from __future__ import annotations

import asyncio

from clause_analysis import ClauseAnalyzer, ClauseInput
from rag.providers.zeus import ZeusProvider


def test_judgment_only_path_keeps_core_requirements():
    """无需内化 short-circuit: coverage fields stay empty, core content is kept."""
    from clause_analysis.analyzer import _base
    from clause_analysis.judge import Judgment

    clause = ClauseInput(term_name="第五十条", term_content="本办法自2026年1月1日起施行。")
    res = _base(
        clause,
        Judgment(decision="无需内化", reason="附则条款", core_requirements="自2026年1月1日起施行"),
        keyword="施行",
    )
    assert res.core_requirements == "自2026年1月1日起施行"
    assert res.coverage_status is None
    assert res.difference_analysis == ""
    assert res.remedy_suggestion == ""

    esg = res.to_esg()
    assert esg["coreRequirements"] == "自2026年1月1日起施行"
    assert esg["coverageStatus"] == ""
    assert esg["differenceAnalysis"] == ""
    assert esg["remedySuggestion"] == ""


def test_degraded_path_backfills_core_requirements_from_judgment():
    """Episode failure keeps the 缺失 + human-review marker but not an empty core requirement."""
    from clause_analysis.analyzer import _degraded
    from clause_analysis.judge import Judgment

    clause = ClauseInput(term_name="第三条", term_content="应当每年至少开展一次风险评估。")
    res = _degraded(
        clause,
        Judgment(decision="需要内化", reason="义务性条款", core_requirements="每年开展一次风险评估"),
        keyword="风险评估",
        note="该条款比对超时,需人工复核。",
    )
    assert res.core_requirements == "每年开展一次风险评估"
    assert res.coverage_status is not None
    assert res.analysis == "该条款比对超时,需人工复核。"


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
