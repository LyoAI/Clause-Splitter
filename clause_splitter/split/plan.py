"""批次规划 —— 基于复杂度动态规划拆分批次.

复杂条款(complexity_score >= complex_solo_threshold)单独成批;
简单条款在 字符数 / 预估义务数 约束内分组。
"""

from __future__ import annotations

from typing import Any

from clause_splitter.config import get_settings
from clause_splitter.utils import get_logger

logger = get_logger()


def plan_batches(articles: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """把条款列表规划为拆分批次。

    Args:
        articles: ArticleInfo 的 dict 列表(含 complexity_score / full_text /
                  estimated_obligation_count)。

    Returns:
        batches: list[list[dict]],每批是一组条款 dict。
    """
    if not articles:
        return []

    settings = get_settings()
    logger.info("Planning batches for %d articles...", len(articles))

    batches: list[list[dict]] = []
    current_batch: list[dict] = []
    current_chars = 0
    current_obls = 0

    for article in articles:
        complexity = article.get("complexity_score", 1.0)
        char_count = len(article.get("full_text", ""))
        est_obl = article.get("estimated_obligation_count", 1)

        # 复杂条款单独成批
        if complexity >= settings.complex_solo_threshold:
            if current_batch:
                batches.append(current_batch)
                current_batch, current_chars, current_obls = [], 0, 0
            batches.append([article])
            continue

        # 简单条款:检查约束
        if (current_chars + char_count > settings.max_batch_chars
                or current_obls + est_obl > settings.max_batch_obligations):
            if current_batch:
                batches.append(current_batch)
            current_batch = [article]
            current_chars = char_count
            current_obls = est_obl
        else:
            current_batch.append(article)
            current_chars += char_count
            current_obls += est_obl

    if current_batch:
        batches.append(current_batch)

    logger.info(
        "Batches planned: %d articles → %d batches (%.0f%% complex solo)",
        len(articles), len(batches),
        sum(1 for b in batches if len(b) == 1) / max(len(batches), 1) * 100,
    )
    return batches
