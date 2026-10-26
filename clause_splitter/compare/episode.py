"""Clause episode — one external clause = one agent episode.

The question carries: 制度名 / section / clause number / clause text plus the
obligation elements split from the clause (subject / action_verb /
action_target / obligation_rule / trigger_condition), so the agent can judge
whether each obligation is adopted by an internal clause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExternalClause:
    """One external clause — the input unit of one comparison.

    ``clause_type`` / ``internalization_decision`` / ``internalization_reason``
    are filled by the pre-internalization stage (before retrieval); empty
    means not yet judged (conservatively treated as needing internalization
    and counted in coverage).
    """

    document_name: str = ""
    clause_number: str = ""
    section: str = ""
    full_text: str = ""
    obligations: list[dict[str, Any]] = field(default_factory=list)
    clause_type: str = ""
    internalization_decision: str = ""
    internalization_reason: str = ""

    @property
    def clause_id(self) -> str:
        """Global clause id, e.g. '网络数据安全风险评估办法#第一条'."""
        doc = self.document_name or "未命名外规"
        return f"{doc}#{self.clause_number or '全文'}"


@dataclass
class EpisodeResult:
    """Compare-domain view of an episode — an adapter over ``AgentResult``.

    The agent's ``answer`` is the verbatim final assistant message; the compare
    side calls it ``raw_answer`` and the ``<answer>`` JSON is parsed by
    ``ground.parse_answer_json`` (the loop carries no answer parser — the
    answer contract is business logic and stays clause-side).
    """

    question: str
    raw_answer: str
    trajectory: list  # list[clause_splitter.agent.AgentTurn]
    messages: list[dict[str, Any]] = field(default_factory=list)
    num_tool_calls: int = 0
    total_tokens: int = 0
    terminated: str = "answer"  # "answer" | "max_tool_calls" | "error"


def _obligation_summary(obligations: list[dict[str, Any]], max_items: int = 12) -> str:
    """Compress the clause's split obligations into summary lines."""
    if not obligations:
        return "（无拆分义务记录）"
    lines = []
    for o in obligations[:max_items]:
        rule = str(o.get("obligation_rule") or o.get("义务规则") or "")
        subject = str(o.get("subject") or o.get("义务主体") or "/")
        verb = str(o.get("action_verb") or o.get("义务动作") or "/")
        target = str(o.get("action_target") or o.get("义务事项") or "/")
        cond = str(o.get("trigger_condition") or o.get("触发义务条件") or "/")
        lines.append(f"- [{rule}] 主体={subject} | 动作={verb} | 事项={target} | 条件={cond}")
    if len(obligations) > max_items:
        lines.append(f"  … 另 {len(obligations) - max_items} 条")
    return "\n".join(lines)


def build_compare_episode_prompt(clause: ExternalClause) -> str:
    """Assemble the episode question (user message) for one external clause."""
    section = clause.section or "/"
    return (
        "请分析以下【外部规定条款】在内部规章制度中的承接情况。\n\n"
        "【外部规定条款】\n"
        f"制度名称: {clause.document_name}\n"
        f"章节: {section}\n"
        f"条款号: {clause.clause_number or '全文'}\n"
        f"条款原文:\n{clause.full_text}\n\n"
        "【该条款拆分出的义务要素】(供判断核心义务,以条款原文为准)\n"
        f"{_obligation_summary(clause.obligations)}\n\n"
        "请检索内规条款库,逐项判断这些义务是否被内规承接,并按答案契约输出 `<answer>` JSON。"
    )


async def run_clause_episode(agent, clause: ExternalClause) -> EpisodeResult:
    """Run one clause episode and map the result to the compare-domain type."""
    r = await agent.arun(build_compare_episode_prompt(clause))
    return EpisodeResult(
        question=r.question,
        raw_answer=r.answer,
        trajectory=r.trajectory,
        messages=r.messages,
        num_tool_calls=r.num_tool_calls,
        total_tokens=r.total_tokens,
        terminated=r.terminated,
    )
