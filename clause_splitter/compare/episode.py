"""条款 episode —— 每条外规条款 = 一个 agent episode.

question 含:制度名 / 章节 / 条款号 / 条款原文 + 该条款拆分出的义务要素摘要
(subject / action_verb / action_target / obligation_rule / trigger_condition),
供 agent 判断每项义务是否被内规承接。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clause_splitter.agent import AgentResult, ReActAgent


@dataclass
class ExternalClause:
    """一条外规条款,作为一次比对的输入单元。"""

    document_name: str = ""
    clause_number: str = ""
    section: str = ""
    full_text: str = ""
    obligations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def clause_id(self) -> str:
        """全局外规条款 id,如 '网络数据安全风险评估办法#第一条'。"""
        doc = self.document_name or "未命名外规"
        return f"{doc}#{self.clause_number or '全文'}"


def _obligation_summary(obligations: list[dict[str, Any]], max_items: int = 12) -> str:
    """把该条款的拆分义务压成摘要行(防 prompt 过长)。"""
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
    """组装一条外规条款的 episode 提问(user 消息)。"""
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


async def run_clause_episode(agent: ReActAgent, clause: ExternalClause) -> AgentResult:
    """跑一条外规条款的 episode。"""
    return await agent.arun(build_compare_episode_prompt(clause))
