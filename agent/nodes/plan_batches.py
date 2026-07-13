"""Batch planning node.

Dynamically plans processing batches based on article complexity scores.
Complex articles get solo batches; simple ones are grouped within constraints.
"""

from __future__ import annotations

from config.settings import get_settings
from utils import get_logger

logger = get_logger()


def plan_batches_node(state: dict) -> dict:
    """Plan decomposition batches. No-op if batches already exist."""

    # Skip if already planned (node may be re-entered during loop)
    existing = state.get("batches", [])
    if existing:
        return {}

    doc_structure = state.get("document_structure")
    if not doc_structure:
        return {"status": "failed", "error_message": "document_structure missing"}

    articles = doc_structure.get("articles", [])
    if not articles:
        return {"status": "failed", "error_message": "No articles found"}

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

        # Complex articles get solo batches
        if complexity >= settings.complex_solo_threshold:
            if current_batch:
                batches.append(current_batch)
                current_batch, current_chars, current_obls = [], 0, 0
            batches.append([article])
            continue

        # Simple articles: check constraints
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

    return {
        "batches": batches,
        "current_batch_index": 0,
        "progress": settings.progress_milestones["batches_planned"],
        "current_step": f"Batches planned: {len(batches)} groups",
        "status": "planning_batches",
    }
