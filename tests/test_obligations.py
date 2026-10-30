"""Split-time obligation five-tuples and the episode prompt that consumes them."""

from __future__ import annotations

import re

from clause_splitter.compare.episode import ExternalClause, build_compare_episode_prompt
from clause_splitter.config import OBLIGATION_MAX_TOKENS
from clause_splitter.split.obligations import (
    build_batches,
    decompose_obligations,
    parse_obligations,
)


def _clause(n: int, text: str = "公司应当建立数据安全制度。") -> ExternalClause:
    return ExternalClause(document_name="外规", clause_number=f"第{n}条", full_text=text)


class _BatchLLM:
    """One obligation per 【clause】 header in the batch listing."""

    def __init__(self, fail_on: int | None = None, break_over: int = 10**6) -> None:
        self.calls = 0
        self.fail_on = fail_on
        self.break_over = break_over  # batches above this size answer truncated JSON
        self.max_tokens: list[int | None] = []

    async def ainvoke(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.max_tokens.append(kwargs.get("max_completion_tokens"))
        if self.calls == self.fail_on:
            raise RuntimeError("llm down")
        numbers = re.findall(r"^【(.+?)】$", messages[-1]["content"], re.MULTILINE)
        if len(numbers) > self.break_over:
            # Simulate a gateway truncation mid-object.
            return _R('{"clauses":[{"clause_number":"%s","obligati' % numbers[0])
        rows = ",".join(
            '{"clause_number":"%s","obligations":[{"obligation_rule":"应当",'
            '"subject":"公司","action_verb":"建立","action_target":"制度",'
            '"trigger_condition":"/"}]}' % n
            for n in numbers
        )
        return _R('{"clauses":[' + rows + "]}")


class _R:
    def __init__(self, content: str) -> None:
        self.content = content
        self.finish_reason = "length"
        self.usage = {"completion_tokens": 4096}


def test_batches_respect_the_char_budget():
    clauses = [_clause(i, "甲" * 1500) for i in range(1, 10)]
    assert [len(b) for b in build_batches(clauses, max_chars=4000)] == [2, 2, 2, 2, 1]


def test_batches_respect_the_clause_budget():
    clauses = [_clause(i) for i in range(1, 26)]
    assert [len(b) for b in build_batches(clauses, max_chars=10**6)] == [6, 6, 6, 6, 1]


def test_batches_give_a_long_clause_its_own_call():
    clauses = [_clause(1, "短" * 10), _clause(2, "长" * 5000), _clause(3, "短" * 10)]
    assert [[c.clause_number for c in b] for b in build_batches(clauses, max_chars=4000)] == [
        ["第1条"], ["第2条"], ["第3条"],
    ]


def test_parse_tolerates_fences_prose_and_empty_rows():
    raw = (
        "拆分结果如下:\n```json\n"
        '{"clauses":[{"clause_number":"第一条","obligations":['
        '{"obligation_rule":"应当","subject":"公司","action_verb":"建立",'
        '"action_target":"制度","trigger_condition":"每年"},'
        '{"obligation_rule":"","subject":"","action_verb":"","action_target":""}]}]}'
        "\n```"
    )
    assert parse_obligations(raw) == {"第一条": [{
        "obligation_rule": "应当", "subject": "公司", "action_verb": "建立",
        "action_target": "制度", "trigger_condition": "每年",
    }]}


def test_parse_returns_empty_when_the_model_narrates():
    assert parse_obligations("抱歉,该条款没有义务。") == {}
    assert parse_obligations('{"clauses": [') == {}


async def test_decompose_fills_every_clause_and_reports_the_call_count():
    clauses = [_clause(i) for i in range(1, 26)]
    llm = _BatchLLM()
    total = await decompose_obligations(clauses, llm, document="外规")
    assert llm.calls == 5  # 6 + 6 + 6 + 6 + 1
    assert total == 25
    assert all(len(c.obligations) == 1 for c in clauses)


async def test_decompose_asks_for_a_generous_output_budget():
    llm = _BatchLLM()
    await decompose_obligations([_clause(1)], llm, document="外规")
    assert llm.max_tokens == [OBLIGATION_MAX_TOKENS]
    assert OBLIGATION_MAX_TOKENS >= 8192  # 4096 truncated multi-clause batches


async def test_decompose_retries_a_truncated_batch_as_halves():
    clauses = [_clause(i) for i in range(1, 7)]
    llm = _BatchLLM(break_over=3)
    total = await decompose_obligations(clauses, llm, document="外规")
    assert total == 6 and all(c.obligations for c in clauses)
    # 1 truncated call + 2 successful halves (no further retry needed).
    assert llm.calls == 3


async def test_decompose_stops_retrying_after_the_depth_limit():
    """A single clause cannot be split further — its failure stays a warning."""
    clauses = [_clause(i) for i in range(1, 5)]
    llm = _BatchLLM(break_over=0)  # every answer truncated, even one-clause batches
    total = await decompose_obligations(clauses, llm, document="外规")
    assert total == 0
    # 1 batch → 2 halves → 4 quarters; a quarter holds one clause and gives up.
    assert llm.calls == 7


async def test_decompose_is_best_effort_on_a_failed_batch():
    clauses = [_clause(i, "甲" * 3000) for i in range(1, 5)]  # one clause per call
    llm = _BatchLLM(fail_on=2)
    total = await decompose_obligations(clauses, llm, document="外规")
    assert total == 3
    assert [bool(c.obligations) for c in clauses] == [True, False, True, True]


async def test_decompose_without_an_llm_does_nothing():
    clauses = [_clause(1)]
    assert await decompose_obligations(clauses, None) == 0
    assert clauses[0].obligations == []


def test_episode_prompt_carries_the_obligation_block():
    clause = _clause(1)
    clause.obligations = [{
        "obligation_rule": "应当", "subject": "数据处理者", "action_verb": "开展",
        "action_target": "风险评估", "trigger_condition": "每年一次",
    }]
    prompt = build_compare_episode_prompt(clause)
    assert "【该条款拆分出的义务要素】" in prompt
    assert "- [应当] 主体=数据处理者 | 动作=开展 | 事项=风险评估 | 条件=每年一次" in prompt
    # The model must not be asked to decompose again (that burned the answer budget).
    assert "拆成若干条具体义务" not in prompt


def test_episode_prompt_degrades_without_obligations():
    prompt = build_compare_episode_prompt(_clause(1))
    assert "(无拆分义务记录)" in prompt
