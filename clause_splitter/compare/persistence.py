"""Persist one agent episode to disk for post-hoc analysis / grounding."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from clause_splitter.agent import AgentResult, AgentTurn


def persist_result(
    result: AgentResult,
    out_dir: str | Path,
    *,
    searcher: str = "",
    query_id: str | None = None,
) -> Path:
    """Write one episode under ``out_dir``; returns the path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tool_call_counts: dict[str, int] = {}
    retrieved: list[str] = []
    retrieved_chunks: list[str] = []
    search_rankings: list[list[dict]] = []
    for turn in result.trajectory:
        tool_call_counts[turn.tool_name] = tool_call_counts.get(turn.tool_name, 0) + 1
        rows = _rows(turn)
        chunk_ids = list(dict.fromkeys(
            str(row["chunk_id"]) for row in rows if row.get("chunk_id")
        ))
        for doc_id in (cid.split("#", 1)[0] for cid in chunk_ids):
            if doc_id not in retrieved:
                retrieved.append(doc_id)
        for chunk_id in chunk_ids:
            if chunk_id not in retrieved_chunks:
                retrieved_chunks.append(chunk_id)
        if turn.tool_name.endswith("_search") and chunk_ids:
            scores = {str(row["chunk_id"]): row.get("score") for row in rows if row.get("chunk_id")}
            search_rankings.append([{"chunk_id": cid, "score": scores.get(cid)} for cid in chunk_ids])

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    name = f"run_{query_id}.json" if query_id else f"run_{stamp}.json"
    path = out_dir / name
    path.write_text(
        json.dumps(
            {
                "metadata": {"searcher": searcher},
                "query_id": query_id,
                "question": result.question,
                "answer": result.answer,
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


def _rows(turn: AgentTurn) -> list[dict[str, Any]]:
    """Rows of a JSON array tool output (empty for anything else)."""
    try:
        rows = json.loads(turn.tool_result)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]
