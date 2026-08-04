"""Gap analysis summary prompt — for summary service (one-shot LLM).

Generates a professional compliance gap analysis report from comparison results.
The core report structure is defined in skills/summary/SKILL.md.
"""

from __future__ import annotations

import json
from datetime import datetime

from prompts.loader import PromptLoader


def get_system_prompt() -> str:
    """Load the summary system prompt from skills/summary/SKILL.md."""
    loader = PromptLoader()
    return loader.load_skill_content("summary").replace(
        "{datetime}", datetime.now().strftime("%Y年%m月%d日 %H:%M")
    )


def format_user_prompt(
    compare_result_json: str,
    external_text_md: str = "",
    internal_text_md: str = "",
) -> str:
    """Build the user prompt with comparison results and original texts.

    Args:
        compare_result_json: JSON string of MatchResult list.
        external_text_md: External regulation text (markdown format).
        internal_text_md: Internal regulation text (markdown format).

    Returns:
        Formatted user prompt string.
    """
    import json as _json

    # Pretty-print the compare results for readability
    try:
        parsed = _json.loads(compare_result_json)
        pretty = _json.dumps(parsed, ensure_ascii=False, indent=2)
    except _json.JSONDecodeError:
        pretty = compare_result_json

    prompt_parts = [
        "## 对比分析结果",
        "",
        "```json",
        pretty,
        "```",
        "",
        "## 外部规定条款",
        "",
        external_text_md or "（原文未提供）",
        "",
        "## 内部规章制度",
        "",
        internal_text_md or "（原文未提供）",
    ]

    return "\n".join(prompt_parts)
