"""Clause comparison prompt — for compare service (one-shot LLM).

Matches split external obligations against pre-split internal obligations
using semantic matching. The core matching logic is defined in skills/compare/SKILL.md.
"""

from __future__ import annotations

import json

from agent.prompts.loader import PromptLoader


def get_system_prompt() -> str:
    """Load the compare system prompt from skills/compare/SKILL.md."""
    loader = PromptLoader()
    return loader.load_skill_content("compare")


def format_user_prompt(
    external_obligations: list[dict],
    internal_obligations: list[dict],
    external_raw_text: str = "",
    internal_raw_text: str = "",
) -> str:
    """Build the user prompt with external and internal obligation data.

    Args:
        external_obligations: List of external ClauseObligation dicts.
        internal_obligations: List of internal ClauseObligation dicts.
        external_raw_text: (optional) External regulation full text.
        internal_raw_text: (optional) Internal regulation full text.

    Returns:
        Formatted user prompt string.
    """
    # Format obligations as readable JSON
    ext_json = json.dumps(external_obligations, ensure_ascii=False, indent=2)
    int_json = json.dumps(internal_obligations, ensure_ascii=False, indent=2)

    prompt_parts = [
        "## 外部规定条款",
        "",
        "外规条款原文：",
        external_raw_text or "（原文未提供，请参考拆分结果）",
        "",
        "外规条款拆分结果：",
        "```json",
        ext_json,
        "```",
        "",
        "## 内部规章制度",
        "",
        "内部条款原文：",
        internal_raw_text or "（原文未提供，请参考拆分结果）",
        "",
        "内部条款拆分结果：",
        "```json",
        int_json,
        "```",
    ]

    return "\n".join(prompt_parts)
