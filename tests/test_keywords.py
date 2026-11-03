"""Keyword extraction: regex rules, llm mode, and the rules fallback (pure logic)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from clause_analysis.keywords import extract_keywords, regex_keywords
from clause_analysis.types import ClauseInput


def _clause(text: str) -> ClauseInput:
    return ClauseInput(term_name="第一条", term_content=text)


class _StubLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return SimpleNamespace(content=self.content)


class _BoomLLM:
    async def ainvoke(self, messages):
        raise RuntimeError("llm down")


def test_regex_matches_lexicon_and_suffix_phrases():
    kws = regex_keywords("网络运营者应当开展风险评估,履行数据安全保护义务,建立应急管理制度。").split("、")
    assert "风险评估" in kws
    assert "数据安全" in kws
    assert "应急管理" in kws          # suffix phrase, leading verb stripped
    assert all("营者" not in k for k in kws)  # phrases wrapping lexicon hits are dropped


def test_regex_orders_by_first_appearance_and_dedupes():
    kws = regex_keywords("落实数据安全要求,开展风险评估;数据安全事件须报送。").split("、")
    assert kws.index("数据安全") < kws.index("风险评估")
    assert len(kws) == len(set(kws))


def test_regex_caps_at_limit_and_returns_empty_without_hits():
    assert len(regex_keywords("风险评估、数据安全、审计、备案、审批、稽核、报送、外包义务清单。").split("、")) <= 6
    assert regex_keywords("没有可提取内容的普通句子。") == ""
    assert regex_keywords("") == ""


def test_llm_mode_parses_term_keyword():
    llm = _StubLLM('<answer>{"term_keyword":"数据安全、报送义务"}</answer>')
    kw = asyncio.run(extract_keywords(llm, _clause("应当报送。"), mode="llm", timeout_s=5))
    assert kw == "数据安全、报送义务"
    assert llm.calls == 1


def test_llm_mode_unparseable_output_falls_back_to_rules():
    llm = _StubLLM("<answer>not json</answer>")
    kw = asyncio.run(extract_keywords(llm, _clause("应当履行数据安全保护义务。"), mode="llm", timeout_s=5))
    assert llm.calls == 1
    assert "数据安全" in kw.split("、")


def test_llm_mode_exception_falls_back_to_rules():
    kw = asyncio.run(extract_keywords(_BoomLLM(), _clause("应当履行数据安全保护义务。"), mode="llm", timeout_s=5))
    assert "数据安全" in kw.split("、")


def test_unknown_mode_is_treated_as_rules_without_calling_llm():
    llm = _StubLLM("whatever")
    kw = asyncio.run(extract_keywords(llm, _clause("应当履行数据安全保护义务。"), mode="bogus", timeout_s=5))
    assert llm.calls == 0
    assert "数据安全" in kw.split("、")


def test_relation_keyword_prefers_episode_keywords_and_falls_back_to_rules():
    from types import SimpleNamespace

    from clause_analysis.analyzer import _relation_keyword
    from clause_analysis.grounding import ground

    verdict = ground('<answer>{"status":"完全覆盖","relation_keywords":{"#a":"模型词1、模型词2"}}</answer>')
    chunk = SimpleNamespace(text="网络运营者应当履行数据安全保护义务。")

    assert _relation_keyword("#a", chunk, verdict, "llm") == "模型词1、模型词2"      # episode wins
    assert "数据安全" in _relation_keyword("#b", chunk, verdict, "llm").split("、")  # missing -> regex
    assert "数据安全" in _relation_keyword("#c", chunk, verdict, "rules").split("、")
    assert _relation_keyword("#a", None, verdict, "llm") == ""
