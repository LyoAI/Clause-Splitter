"""Small text helpers shared across pipeline and comparison code."""

from __future__ import annotations

import re


def norm_str(value: object) -> str:
    """Lenient str getter: missing / non-str → "" so new fields never break a run."""
    if isinstance(value, str):
        return value.strip()
    return ""


def segment_label(chunk) -> str:
    """The internal chunk's clause/paragraph label.

    Prefers the last leaf of ``meta.section`` ("第三章 数据安全 > 第二十条" →
    "第二十条"; Excel libraries store "第一条" directly — returned as-is).
    Without a section, extract a leading "第X条" from the chunk text; last
    resort is a 1-based paragraph number (``第N段``).
    """
    section = str(chunk.meta.get("section", "") or "").strip() if chunk.meta else ""
    if section:
        leaf = section.split(" > ")[-1].strip()
        if leaf:
            return leaf
    head = re.match(r"^\s*(第[一二三四五六七八九十百零\d]+条)", chunk.text or "")
    if head:
        return head.group(1)
    if "#" in chunk.chunk_id:
        order = chunk.chunk_id.rsplit("#", 1)[-1]
        if order.isdigit():
            return f"第{int(order) + 1}段"
    return ""
