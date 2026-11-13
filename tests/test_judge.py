"""Internalization judgment parsing (no retrieval, no LLM network)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from clause_analysis.judge import judge
from clause_analysis.types import ClauseInput, NO_INTERNALIZATION


class _StubLLM:
    def __init__(self, content: str) -> None:
        self.content = content

    async def ainvoke(self, messages):
        return SimpleNamespace(content=self.content)


def _judge(answer_json: str):
    clause = ClauseInput(term_name="第一条", term_content="本办法自2026年1月1日起施行。")
    return asyncio.run(judge(_StubLLM(f"<answer>{answer_json}</answer>"), clause, timeout_s=5))


def test_core_requirements_parsed_for_declarative_clause():
    j = _judge(
        '{"internalization_decision":"无需内化","internalization_reason":"附则",'
        '"requireType":"鼓励性要求","core_regulatory_requirements":"本办法自2026年1月1日起施行"}'
    )
    assert j.decision == NO_INTERNALIZATION
    assert j.core_requirements == "本办法自2026年1月1日起施行"


def test_core_requirements_default_empty_when_field_missing():
    j = _judge('{"internalization_decision":"无需内化","internalization_reason":"附则"}')
    assert j.decision == NO_INTERNALIZATION
    assert j.core_requirements == ""


def test_judge_failure_fails_open_to_needs_internalization():
    clause = ClauseInput(term_name="第一条", term_content="应当建立制度。")

    class _Boom:
        async def ainvoke(self, messages):
            raise RuntimeError("llm down")

    j = asyncio.run(judge(_Boom(), clause, timeout_s=5))
    assert j.decision != NO_INTERNALIZATION
    assert j.core_requirements == ""
