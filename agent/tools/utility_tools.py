"""Utility tools — complexity estimation, token counting, etc."""

from __future__ import annotations

import re
from typing import Any

from config.settings import get_settings


# ── Complexity estimation ──────────────────────────────────────────────────

def estimate_complexity(article_text: str) -> dict[str, Any]:
    """Estimate clause complexity on a 0-10 scale using structural heuristics.

    Only uses language-agnostic structural metrics — NO hardcoded verb lists
    or keyword dictionaries. The LLM is the primary complexity judge; this
    function is a lightweight fallback for when LLM is unavailable.

    Metrics:
    - Character count (longer = more complex)
    - Punctuation density: commas + semicolons (more clauses = more complex)
    - Sub-item count: (一)(二), 1. 2., etc.
    - Sentence count: periods + paragraph breaks

    Args:
        article_text: Full text of the article.

    Returns:
        {
            "complexity_score": float (0-10),
            "complexity_label": "simple" | "complex",
            "metrics": {char_count, comma_count, sub_item_count, sentence_count}
        }
    """
    settings = get_settings()
    char_count = len(article_text)

    # Punctuation density — more commas/semicolons → more clauses
    comma_count = article_text.count("，") + article_text.count("；") + article_text.count(",") + article_text.count(";")

    # Sub-item count — (一)(二) or 1. 2. or a) b)
    sub_items = re.findall(r"[（(][一二三四五六七八九十\d]+[）)]", article_text)
    sub_item_count = len(sub_items)

    # Sentence count — periods, Chinese/English
    sentence_count = (
        article_text.count("。") + article_text.count(".") +
        article_text.count("；") + article_text.count(";") +
        article_text.count("\n")
    )

    # ── Score from structural tiers ────────────────────────────────────
    score = 0.0

    # Length (max 3 pts)
    for threshold, points in settings.complexity_length_tiers:
        if char_count > threshold:
            score += points
            break

    # Clause density — commas per 100 chars (max 3 pts)
    density = comma_count / max(char_count, 1) * 100
    if density > 8:
        score += 3.0
    elif density > 4:
        score += 2.0
    elif density > 1:
        score += 1.0

    # Sub-items (max 2 pts)
    for threshold, points in settings.complexity_sub_item_tiers:
        if sub_item_count > threshold:
            score += points
            break

    # Long sentences (max 2 pts)
    avg_sentence_len = char_count / max(sentence_count, 1)
    if avg_sentence_len > 150:
        score += 2.0
    elif avg_sentence_len > 80:
        score += 1.0

    score = min(score, 10.0)

    return {
        "complexity_score": round(score, 1),
        "complexity_label": "complex" if score >= settings.complex_solo_threshold else "simple",
        "metrics": {
            "char_count": char_count,
            "comma_count": comma_count,
            "sub_item_count": sub_item_count,
            "sentence_count": sentence_count,
        },
    }


# ── Token estimation ───────────────────────────────────────────────────────

def count_tokens(text: str, model: str = "default") -> dict[str, Any]:
    """Estimate token count using heuristics.

    Chinese: ~1.5 chars/token. English: ~4 chars/token.

    Args:
        text: Text to estimate.
        model: Model name (reserved for future per-model calculation).

    Returns:
        {"estimated_tokens": int, "char_count": int, "method": str}
    """
    char_count = len(text)
    chinese_chars = len(re.findall(r"[一-鿿]", text))
    non_chinese = char_count - chinese_chars
    estimated = int(chinese_chars / 1.5 + non_chinese / 4.0)

    return {
        "estimated_tokens": max(estimated, 1),
        "char_count": char_count,
        "method": "heuristic (~1.5 chars/token CN, ~4 chars/token EN)",
    }

