"""One-line bilingual tool hints for the system prompt's ``{tools}`` slot."""

from __future__ import annotations

from typing import Iterable

SEARCH_TOOL = "knowledge_search"

_HINTS: dict[str, dict[str, str]] = {
    SEARCH_TOOL: {
        "zh": "在内部规章制度库中混合检索（语义+关键词），输入自然语言问题或关键词",
        "en": "hybrid (semantic + lexical) search over the internal regulation corpus",
    },
    "get_context": {
        "zh": "读取 chunk 附近窗口的完整文本，用于确认检索片段是否足够",
        "en": "return the full text of a chunk window around chunk_id",
    },
    "get_materials": {
        "zh": "列出内规库中可用的制度文件名",
        "en": "list the internal documents available in the corpus",
    },
    "calculate": {
        "zh": "计算算术表达式并返回精确数值；任何计算都用它，不要手算",
        "en": "evaluate an arithmetic expression and return the exact result",
    },
}


def build_tool_hints(tool_names: Iterable[str], lang: str = "zh") -> str:
    """Bulleted ``- name: hint`` list in ``lang``."""
    key = "en" if lang.lower() == "en" else "zh"
    sep = ": " if key == "en" else "："
    return "\n".join(
        f"- {name}{sep}{_HINTS[name][key]}" for name in tool_names if name in _HINTS
    )
