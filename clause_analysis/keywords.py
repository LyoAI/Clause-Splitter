"""Keyword extraction for one clause — the step before internalization analysis.

Both 内规 and 外规 clauses need keywords, so this runs for every clause
regardless of ``need_internalization``. Two modes:

- ``rules``: lexicon + suffix-pattern regex, ms-level (the default);
- ``llm``:   one small LLM call; any failure falls back to ``rules``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from utils import get_logger, norm_str

from .grounding import parse_answer_json
from .prompts import render
from .types import ClauseInput

logger = get_logger()

# Compliance-domain lexicon; hits are ordered by first appearance in the text.
_LEXICON = (
    "数据安全", "个人信息保护", "网络安全", "重要数据", "核心数据", "数据分类分级",
    "风险评估", "风险监测", "应急处置", "应急预案", "报送", "备案", "审批", "核准",
    "审计", "稽核", "信息披露", "跨境", "出境", "外包", "集中采购", "隐私保护",
    "算法治理", "算法备案", "算法评估", "深度合成", "人工智能", "区块链", "云计算",
    "访问控制", "权限管控", "日志留存", "加密", "脱敏", "消费者权益保护",
    "反洗钱", "反垄断", "关联交易", "内部控制", "合规管理", "问责", "整改",
)

# Chinese noun phrases ending in obligation-ish suffixes, as fallback beyond the lexicon.
_SUFFIX_RE = re.compile(
    r"[一-龥]{2,8}?(?:义务|责任|要求|制度|机制|流程|评估|审查|审核|备案|审批|报送|监督|管理|审计|报告)"
)
# Verb/connector prefixes stripped from the front of suffix phrases.
_LEAD_RE = re.compile(
    r"^(?:应当|必须|不得|禁止|鼓励|支持|并|及|或者|或|和|与|依法|定期|及时|严格|切实"
    r"|加强|完善|落实|开展|履行|建立|健全|制定|组织|进行|做好|采取)+"
)


def regex_keywords(text: str, limit: int = 6) -> str:
    """Deterministic extraction: lexicon hits then suffix phrases, deduped, capped."""
    hits: list[str] = []
    text = text or ""
    found = sorted((text.find(t), t) for t in _LEXICON if t in text)
    hits.extend(t for _, t in found)

    def _add(phrase: str) -> None:
        stripped = _LEAD_RE.sub("", phrase)
        # A phrase wrapping an already-hit lexicon term is boundary noise, skip it.
        if not stripped or any(k in stripped for k in hits) or stripped in hits:
            return
        hits.append(stripped)

    for m in _SUFFIX_RE.finditer(text):
        _add(m.group(0))
    return "、".join(hits[:limit])


async def extract_keywords(llm: Any, clause: ClauseInput, *, mode: str, timeout_s: float) -> str:
    """Keywords for one clause. ``llm`` mode falls back to rules on any failure."""
    if mode == "llm":
        try:
            resp = await asyncio.wait_for(
                llm.ainvoke([
                    {"role": "system", "content": render("keywords")},
                    {"role": "user", "content": _keywords_prompt(clause)},
                ]),
                timeout=timeout_s,
            )
        except Exception as e:  # noqa: BLE001 — keywords must never break the flow
            logger.warning("keyword LLM failed for %s; falling back to rules: %s", clause.term_name, e)
        else:
            parsed = parse_answer_json(resp.content or "")
            keyword = norm_str(parsed.get("term_keyword")) if parsed else ""
            if keyword:
                return keyword
            logger.warning("keyword LLM output empty/unparseable for %s; falling back to rules", clause.term_name)
    # "rules" and any unknown mode value land here (fail-open).
    return regex_keywords(clause.term_content)


def _keywords_prompt(clause: ClauseInput) -> str:
    return (
        "【条款原文】\n"
        f"{clause.term_content}\n\n"
        "请按输出契约提取该条款的核心合规关键词。"
    )
