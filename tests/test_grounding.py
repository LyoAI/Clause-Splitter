"""Verdict pass-through, answer parsing and the ESG element shape (pure logic)."""

from __future__ import annotations

import json

import pytest

from clause_analysis.episode import build_episode_prompt
from clause_analysis.grounding import ground, parse_answer_json, resolve_ids
from clause_analysis.types import ClauseInput, ClauseResult, CoverageStatus, RelatedTerm
from agent import ToolRegistry
from rag import Hit
from rag.providers.zeus import ZeusProvider


def _answer(payload: dict) -> str:
    return f"<answer>{json.dumps(payload, ensure_ascii=False)}</answer>"


def test_parse_answer_tolerates_missing_close_tag_and_trailing_prose():
    raw = '<answer>{"status":"完全覆盖"} 以上是结论'
    assert parse_answer_json(raw) == {"status": "完全覆盖"}


def test_parse_answer_returns_none_for_garbage():
    assert parse_answer_json("<answer>not json</answer>") is None
    assert parse_answer_json("") is None


def test_verdict_passes_through_even_when_claimed_ids_are_unknown():
    verdict = ground(_answer({
        "status": "部分覆盖",
        "matched_internal_clause_ids": ["never-seen#9"],
        "analysis": "1) 遗漏子要求",
        "difference_analysis": "1) 时限更松",
    }))
    assert verdict.status == CoverageStatus.PARTIALLY_COVERED
    assert verdict.claimed_ids == ["never-seen#9"]
    assert verdict.difference_analysis == "1) 时限更松"


def test_missing_status_degrades_to_not_covered_with_default_texts():
    verdict = ground("no answer block at all")
    assert verdict.status == CoverageStatus.NOT_COVERED
    assert verdict.analysis and verdict.remedy_suggestion


def test_resolve_ids_fills_display_fields_from_this_calls_cache():
    registry = ToolRegistry(ZeusProvider())  # unconfigured; cache is all we use
    registry.remember([Hit(chunk_id="数据安全办法#2", text="第二条 内容", doc_id="数据安全办法")])
    resolved = dict(resolve_ids(["数据安全办法#2", "ghost#1"], registry))
    assert resolved["数据安全办法#2"] is not None
    assert resolved["数据安全办法#2"].text == "第二条 内容"
    assert resolved["ghost#1"] is None


def test_episode_prompt_carries_clause_text_only():
    clause = ClauseInput(document_name="办法", clause_number="第一条", section="第一章", text="应当建立制度。")
    prompt = build_episode_prompt(clause)
    assert "应当建立制度。" in prompt
    assert "第一章" in prompt
    assert "义务要素" not in prompt


def test_to_esg_maps_algorithm_fields_and_empty_when_not_analysed():
    analysed = ClauseResult(
        clause_number="第一条",
        internalize="需要内化",
        internalize_reason="义务性条款",
        require_type="禁止性要求",
        coverage_status=CoverageStatus.FULLY_COVERED,
        analysis="1) 已承接",
        related_terms=[RelatedTerm(
            ai_relation_basis="关键句", relation_doc_id="doc-9", term_content="…",
            term_id="办法#2", term_name="第二条",
        )],
    ).to_esg()
    assert analysed["termName"] == "第一条"
    assert analysed["internalize"] == "1"
    assert analysed["requireType"] == "2"
    assert analysed["coverageStatus"] == "2"
    assert analysed["relationTerms"] == [{
        "aiRelationBasis": "关键句", "relationDocId": "doc-9", "termContent": "…",
        "termId": "办法#2", "termKeyword": "", "termName": "第二条",
    }]

    skipped = ClauseResult(clause_number="第二条", internalize="无需内化", internalize_reason="宣示性").to_esg()
    assert skipped["internalize"] == "0"
    assert skipped["coverageStatus"] == ""
    assert skipped["requireType"] == ""
    assert skipped["analysis"] == ""
    assert skipped["relationTerms"] == []


def test_coverage_and_require_type_digit_codes():
    from clause_analysis.types import COVERAGE_CODE, REQUIRE_TYPE_CODE

    assert COVERAGE_CODE[CoverageStatus.NOT_COVERED] == "0"
    assert COVERAGE_CODE[CoverageStatus.PARTIALLY_COVERED] == "1"
    assert COVERAGE_CODE[CoverageStatus.FULLY_COVERED] == "2"
    assert REQUIRE_TYPE_CODE == {"义务性要求": "1", "禁止性要求": "2", "鼓励性要求": "3"}


def test_evidence_is_parsed_per_chunk_id():
    verdict = ground(_answer({
        "status": "完全覆盖",
        "matched_internal_clause_ids": ["办法#2"],
        "evidence": {"办法#2": "第二条 原文关键句"},
    }))
    assert verdict.evidence == {"办法#2": "第二条 原文关键句"}


def test_failed_search_is_counted_so_a_miss_is_not_trusted():
    from rag import RagNotConfigured

    registry = ToolRegistry(ZeusProvider())  # unconfigured -> every search raises
    with pytest.raises(RagNotConfigured):
        registry.search("数据安全")
    assert registry.failed_searches == 1
