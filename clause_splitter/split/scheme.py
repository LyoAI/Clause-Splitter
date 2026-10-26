"""Scheme-based structure inference — the LLM proposes a numbering scheme,
code validates and applies it (split ladder layers ①/②).

The LLM never segments text: one structured call outputs the document's
numbering system (pattern + level + kind). ``apply_scheme`` re-classifies
blocks under that scheme — a generalized ``compare.headings``. The builtin
regex scheme (headings.py) stays the fallback when validation fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from clause_splitter.config import CHUNK_LEVEL
from clause_splitter.parsing.blocks import Block
from clause_splitter.prompts import render
from clause_splitter.utils import get_logger
from clause_splitter.utils.json_utils import parse_json_from_text

logger = get_logger()

# Compressed block listing fed to the LLM (index | type | text prefix).
_TEXT_PREFIX_CHARS = 120
_LISTING_CAP = 8000
#: Article-pattern hits required to trust an inferred scheme.
_MIN_ARTICLE_HITS = 3
#: A clause longer than this (chars) means the split failed, not the doc.
_MONSTER_CLAUSE_CHARS = 8000


@dataclass
class SchemeRule:
    """One numbering marker: line-start regex + heading level + role."""

    pattern: re.Pattern
    level: int
    kind: str  # "article" | "section"

    def to_json(self) -> dict:
        return {"pattern": self.pattern.pattern, "level": self.level, "kind": self.kind}


# ── layer ①: LLM scheme inference ──────────────────────────────────────


async def infer_scheme_llm(blocks: list[Block], llm: Any) -> list[SchemeRule] | None:
    """One structured call → validated rules, or None (fall to layer ③)."""
    if llm is None:
        return None
    listing = "\n".join(
        f"{i}|{b.type}|{b.text.strip()[:_TEXT_PREFIX_CHARS]}" for i, b in enumerate(blocks)
    )
    if len(listing) > _LISTING_CAP:
        listing = listing[:_LISTING_CAP] + "\n…(截断)"
    try:
        resp = await llm.ainvoke([
            {"role": "system", "content": render("split_scheme")},
            {"role": "user", "content": listing},
        ])
        content = getattr(resp, "content", None) or ""
        parsed = parse_json_from_text(content)
        rules = sanitize_scheme(parsed.get("patterns") if isinstance(parsed, dict) else None, blocks)
    except Exception as exc:  # noqa: BLE001 — any LLM/parse failure falls to ③
        logger.warning("scheme inference failed, using builtin scheme: %s", exc)
        return None
    if rules:
        logger.info("scheme inferred: %s", [r.to_json() for r in rules])
    return rules


# ── layer ②: validation + application ──────────────────────────────────


def sanitize_scheme(raw: Any, blocks: list[Block]) -> list[SchemeRule] | None:
    """Hard gate: patterns compile, levels unique in 1..CHUNK_LEVEL,
    ≥1 article rule, articles deepest, and enough article hits on this doc."""
    if not isinstance(raw, list) or not raw:
        return None
    rules: list[SchemeRule] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        try:
            pattern = re.compile(str(item.get("pattern") or ""))
        except re.error:
            return None
        level, kind = item.get("level"), item.get("kind")
        if not isinstance(level, int) or not 1 <= level <= CHUNK_LEVEL:
            return None
        if kind not in ("article", "section"):
            return None
        rules.append(SchemeRule(pattern=pattern, level=level, kind=kind))

    levels = [r.level for r in rules]
    if len(set(levels)) != len(levels):
        return None
    article_levels = [r.level for r in rules if r.kind == "article"]
    if not article_levels:
        return None
    if any(r.level > max(article_levels) for r in rules):
        return None  # a section deeper than every article

    texts = [b.text.strip() for b in blocks]
    hits = sum(
        1 for t in texts for r in rules if r.kind == "article" and r.pattern.match(t)
    )
    if hits < _MIN_ARTICLE_HITS:
        return None
    return sorted(rules, key=lambda r: r.level)


def apply_scheme(blocks: list[Block], rules: list[SchemeRule]) -> list[Block]:
    """Re-classify blocks under a scheme — generalized headings.py loop:
    article markers split into title + body, section markers take the line."""
    from clause_splitter.compare.headings import _looks_like_doc_title

    out: list[Block] = []
    order = 0
    for i, b in enumerate(blocks):
        text = b.text.strip()
        rule = next((r for r in rules if r.pattern.match(text)), None)
        if rule is None:
            if i == 0 and b.type == "paragraph" and _looks_like_doc_title(text):
                out.append(Block(b.block_id, "title", text, b.page, order, 1))
                order += 1
                continue
            out.append(Block(b.block_id, b.type, text, b.page, order))
            order += 1
            continue
        m = rule.pattern.match(text)
        if rule.kind == "article":
            label = m.group(0).strip()
            body = text[m.end():].lstrip("* 　").rstrip()
            out.append(Block(f"{b.block_id}h", "title", label, b.page, order, rule.level))
            order += 1
            if body:
                out.append(Block(f"{b.block_id}b", "paragraph", body, b.page, order))
                order += 1
        else:
            out.append(Block(b.block_id, "title", text.strip("* ").strip(), b.page, order, rule.level))
            order += 1
    return out


def scheme_output_sane(clauses: list) -> bool:
    """>=2 clauses and no monster clause — else the split failed, keep falling."""
    if len(clauses) < 2:
        return False
    return all(len(c.full_text) <= _MONSTER_CLAUSE_CHARS for c in clauses)
