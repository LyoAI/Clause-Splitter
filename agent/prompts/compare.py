"""Clause comparison prompt — for compare service (one-shot LLM).

Matches split external obligations against pre-split internal obligations
using semantic matching. The core matching logic is defined in skills/compare/SKILL.md.

Supports many-to-many: external and internal obligations are grouped
by their source document for clear prompt structure.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from agent.prompts.loader import PromptLoader


def get_system_prompt() -> str:
    """Load the compare system prompt from skills/compare/SKILL.md."""
    loader = PromptLoader()
    return loader.load_skill_content("compare")


def _group_by_document(obligations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group obligations by their source document name.

    Each obligation dict should have a 'document_name' key.
    Obligations without one are grouped under '（未标注文档）'.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for obl in obligations:
        doc = obl.get("document_name", "") or "（未标注文档）"
        grouped[doc].append(obl)
    return dict(grouped)


def _format_document_sections(
    grouped: dict[str, list[dict[str, Any]]],
    raw_texts: dict[str, str],
    section_title: str,
) -> list[str]:
    """Build prompt sections for one side (external or internal).

    Args:
        grouped: {document_name: [obligation_dict, ...]}
        raw_texts: {document_name: raw_text} for optional context
        section_title: "外部规定条款" or "内部规章制度"

    Returns:
        List of text lines for this section.
    """
    total_obls = sum(len(v) for v in grouped.values())
    num_docs = len(grouped)
    lines = [
        f"## {section_title}（共 {total_obls} 条义务，来自 {num_docs} 份文件）",
        "",
    ]

    for doc_name, obligations in grouped.items():
        lines.append(f"### 文件: 《{doc_name}》（{len(obligations)} 条）")
        lines.append("")

        # Optional raw text for context
        raw = raw_texts.get(doc_name, "")
        if raw:
            # Truncate raw text to avoid overwhelming the prompt
            raw_preview = raw[:3000]
            if len(raw) > 3000:
                raw_preview += f"\n...（原文共 {len(raw)} 字符，已截断）"
            lines.append("原文摘要：")
            lines.append(raw_preview)
            lines.append("")

        # Obligations JSON block
        lines.append("条款拆分结果：")
        lines.append("```json")
        lines.append(json.dumps(obligations, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

    return lines


def format_user_prompt(
    external_obligations: list[dict[str, Any]],
    internal_obligations: list[dict[str, Any]],
    external_raw_text: str = "",
    internal_raw_text: str = "",
) -> str:
    """Build the user prompt with external and internal obligation data.

    Obligations are automatically grouped by their 'document_name' field.
    For many-to-many comparisons, this produces clear per-document sections.

    Args:
        external_obligations: List of external ClauseObligation dicts.
            Each dict should have a 'document_name' key.
        internal_obligations: List of internal ClauseObligation dicts.
            Each dict should have a 'document_name' key.
        external_raw_text: (optional) Combined external regulation full text.
        internal_raw_text: (optional) Combined internal regulation full text.

    Returns:
        Formatted user prompt string.
    """
    prompt_parts: list[str] = []

    # ── External obligations ──────────────────────────────────────────────
    ext_grouped = _group_by_document(external_obligations)

    # Build raw text map: if only one doc, use the provided raw text directly
    ext_raw_map: dict[str, str] = {}
    if len(ext_grouped) == 1 and external_raw_text:
        ext_raw_map[next(iter(ext_grouped))] = external_raw_text
    elif external_raw_text:
        # Multiple docs — raw text is combined; put under a general key
        ext_raw_map = {doc: "" for doc in ext_grouped}

    prompt_parts.extend(
        _format_document_sections(ext_grouped, ext_raw_map, "外部规定条款")
    )

    # ── Internal obligations ──────────────────────────────────────────────
    int_grouped = _group_by_document(internal_obligations)

    int_raw_map: dict[str, str] = {}
    if len(int_grouped) == 1 and internal_raw_text:
        int_raw_map[next(iter(int_grouped))] = internal_raw_text
    elif internal_raw_text:
        int_raw_map = {doc: "" for doc in int_grouped}

    prompt_parts.extend(
        _format_document_sections(int_grouped, int_raw_map, "内部规章制度")
    )

    return "\n".join(prompt_parts)
