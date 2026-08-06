"""Deterministic BM25 candidate retrieval for the compare pipeline.

Given one external obligation, build BM25 queries from its structured fields
and full 原文, run them against the internal index, merge the hits (keeping the
max score per docid), and return the top-K candidates for the LLM judge.

This replaces the old ReAct loop in which the LLM *drove* the search turn by
turn. Retrieval is now deterministic, cheap, and reliable — the LLM only judges
the candidates it is handed, which matches the intended flow:
"bm25 召回最相近的内规候选 → LLM 判断是否承接".
"""

from __future__ import annotations

from typing import Any

from utils import get_logger

logger = get_logger()

# Fields whose non-empty values are joined into the structured-field query.
_STRUCTURED_QUERY_FIELDS = ("subject", "action_verb", "action_target")


def build_retrieval_queries(obligation: dict[str, Any]) -> list[str]:
    """Build 1-3 BM25 queries for one external obligation.

    Queries, in order of expected precision:
      1. structured fields — subject + action_verb + action_target
      2. the full 原文 (source_text)
      3. a ~120-char prefix of the 原文, when the text is long (a very long
         query dilutes BM25 term weights, so a short head often matches better)

    Args:
        obligation: One ClauseObligation dict.

    Returns:
        A non-empty list of query strings (empty/placeholder fields skipped).
    """
    queries: list[str] = []

    parts = [
        str(obligation.get(key, "") or "").strip()
        for key in _STRUCTURED_QUERY_FIELDS
    ]
    structured = " ".join(p for p in parts if p and p != "/")
    if structured:
        queries.append(structured)

    source = str(obligation.get("source_text", "") or "").strip()
    if source:
        queries.append(source)
        if len(source) > 120:
            queries.append(source[:120])

    if not queries:
        # Fallback: even a bare obligation should be searchable.
        source = str(obligation.get("source_text", "") or "")
        queries.append(source if source else str(obligation.get("index", "")))

    return queries


def retrieve_candidates(
    searcher: Any,
    obligation: dict[str, Any],
    top_k: int = 12,
) -> list[dict[str, Any]]:
    """Retrieve the top-K internal candidates for one external obligation.

    Runs every query from :func:`build_retrieval_queries`, merges the hits by
    docid keeping the highest score, sorts by score descending, and truncates
    to ``top_k``. Each result dict is ``{docid, score, obligation}`` — the same
    shape ``BM25Searcher.search`` returns, so the caller can read full 原文 and
    ground evidence verbatim.

    Args:
        searcher: BM25Searcher (or any object with ``search(query, k)``).
        obligation: One external ClauseObligation dict.
        top_k: Number of candidates to return (final truncation).

    Returns:
        Sorted candidate list; possibly empty when nothing matched.
    """
    merged: dict[str, dict[str, Any]] = {}
    for query in build_retrieval_queries(obligation):
        if not query.strip():
            continue
        try:
            hits = searcher.search(query, k=top_k)
        except Exception as e:  # noqa: BLE001 — one bad query must not kill the batch
            logger.warning("Retrieval query failed (%r): %s", query[:40], e)
            continue
        for hit in hits:
            docid = hit["docid"]
            current = merged.get(docid)
            if current is None or hit["score"] > current["score"]:
                merged[docid] = hit

    candidates = sorted(merged.values(), key=lambda x: -x["score"])
    return candidates[:top_k]
