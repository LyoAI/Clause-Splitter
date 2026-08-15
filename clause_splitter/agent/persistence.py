"""Persist one agent episode to disk for post-hoc analysis / grounding.

One JSON file per episode carrying per-tool call counts, token usage,
termination status, retrieved doc/chunk ids, and the full OpenAI wire-format
``messages`` transcript — the authoritative trajectory for grounding checks
(``matched_internal_clause_ids`` must be traceable to these tool outputs).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .react_agent import AgentResult, AgentTurn


def persist_result(
    result: AgentResult,
    out_dir: str | Path,
    *,
    model: str = "",
    provider: str = "",
    company: str = "",
    searcher: str = "",
    query_id: str | None = None,
) -> Path:
    """Write one episode under ``out_dir``; returns the path.

    The file is ``run_<query_id>.json`` when a ``query_id`` is known (batch),
    else ``run_<utc-timestamp>.json`` (ad-hoc single runs)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tool_call_counts: dict[str, int] = {}
    retrieved: list[str] = []
    retrieved_chunks: list[str] = []
    search_rankings: list[list[dict]] = []  # per search call: {chunk_id, score} in score order
    for turn in result.trajectory:
        tool_call_counts[turn.tool_name] = tool_call_counts.get(turn.tool_name, 0) + 1
        chunk_ids = _chunkids_in(turn)  # deduped, in the order the tool returned them
        for doc_id in (cid.split("#", 1)[0] for cid in chunk_ids):
            if doc_id not in retrieved:
                retrieved.append(doc_id)
        for chunk_id in chunk_ids:
            if chunk_id not in retrieved_chunks:
                retrieved_chunks.append(chunk_id)
        if turn.tool_name.endswith("_search"):
            ranking = _search_ranking(turn)
            if ranking:
                search_rankings.append(ranking)

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    name = f"run_{query_id}.json" if query_id else f"run_{stamp}.json"
    path = out_dir / name
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "model": model,
                    "provider": provider,
                    "company": company,
                    "searcher": searcher,
                },
                "query_id": query_id,
                "question": result.question,
                "answer": result.answer,
                "unit": result.unit,
                "terminated": result.terminated,
                "tool_call_counts": tool_call_counts,
                "usage": {"total_tokens": result.total_tokens},
                "status": "completed" if result.terminated == "answer" else result.terminated,
                "retrieved_docids": retrieved,
                "retrieved_chunk_ids": retrieved_chunks,
                "search_rankings": search_rankings,
                "messages": result.messages,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _chunkids_in(turn: AgentTurn) -> list[str]:
    """Chunk ids referenced by a search / get_context tool output."""
    try:
        rows = json.loads(turn.tool_result)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    return list(dict.fromkeys(  # dedupe, preserve order
        row["chunk_id"] for row in rows
        if isinstance(row, dict) and row.get("chunk_id")
    ))


def _search_ranking(turn: AgentTurn) -> list[dict]:
    """Ranked ``{chunk_id, score}`` pairs from a search tool's output.

    Search results carry a ``score`` and are returned in score-descending
    order, so this preserves the true retrieval ranking (with the relevance
    score attached for auditability).
    """
    try:
        rows = json.loads(turn.tool_result)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    ranking, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = row.get("chunk_id")
        if cid and cid not in seen:
            seen.add(cid)
            ranking.append({"chunk_id": cid, "score": row.get("score")})
    return ranking
