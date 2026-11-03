"""The comparison episode's question for one clause.

Only the clause text and its metadata go in. The model reads the text and
identifies its obligations itself while judging; nothing is pre-decomposed, so
the answer budget is spent on the verdict.
"""

from __future__ import annotations

from .types import ClauseInput


def build_episode_prompt(clause: ClauseInput) -> str:
    return (
        "请分析以下【外部规定条款】在内部规章制度中的承接情况。\n\n"
        "【外部规定条款】\n"
        f"制度名称: {clause.document_name}\n"
        f"章节: {clause.section or '/'}\n"
        f"条款名称: {clause.term_name or '全文'}\n"
        f"条款原文:\n{clause.term_content}\n\n"
        "请检索内规条款库，逐项判断条款原文中的义务是否被内规承接，"
        "并按答案契约输出 `<answer>` JSON。"
    )
