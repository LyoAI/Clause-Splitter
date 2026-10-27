"""One external clause = one agent episode."""

from __future__ import annotations

from dataclasses import dataclass

from clause_splitter.agent import AgentResult


@dataclass
class ExternalClause:
    """One external clause — the input unit of one comparison.

    ``clause_type`` / ``internalization_decision`` / ``internalization_reason``
    are filled before retrieval; empty means not yet judged (conservatively
    treated as needing internalization and counted in coverage).
    """

    document_name: str = ""
    clause_number: str = ""
    section: str = ""
    full_text: str = ""
    clause_type: str = ""
    internalization_decision: str = ""
    internalization_reason: str = ""

    @property
    def clause_id(self) -> str:
        """Global clause id, e.g. '网络数据安全风险评估办法#第一条'."""
        doc = self.document_name or "未命名外规"
        return f"{doc}#{self.clause_number or '全文'}"


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
        "请先在内部把该条款拆成若干条具体义务(主体/动作/事项/触发条件),"
        "再检索内规条款库,逐项判断这些义务是否被内规承接,并按答案契约输出 `<answer>` JSON。"
    )


async def run_clause_episode(agent, clause: ExternalClause) -> AgentResult:
    """Run one clause episode (the answer contract is parsed in ``ground``)."""
    return await agent.arun(build_compare_episode_prompt(clause))
