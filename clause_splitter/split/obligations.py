"""Obligation five-tuples extracted at split time (external regulations only).

The comparison prompt hands the model a precomputed obligation list so it only
has to retrieve and judge. Asking the episode model to decompose inline invites
narration, which eats the answer budget and truncates the JSON — so the split
stage pays for it once, batched, instead of every episode paying for it again.

Cost: one LLM call per batch (~2500 chars of clause text, ≤6 clauses), run
two at a time. Internal/attachment documents skip this entirely.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from clause_splitter.compare.episode import ExternalClause
from clause_splitter.config import OBLIGATION_MAX_TOKENS
from clause_splitter.prompts import render
from clause_splitter.utils import get_logger

logger = get_logger()

#: Batch budgets — one call carries at most this much clause text / clauses.
#: Kept small because the gateway truncates long JSON answers mid-object.
MAX_BATCH_CHARS = 2500
MAX_BATCH_CLAUSES = 6
#: Concurrent batches (matches the 2-slot intranet LLM channel).
CONCURRENCY = 2
#: Unparsed batch of N>1 clauses is retried as halves, at most this many levels.
MAX_SPLIT_RETRIES = 2
#: Per-clause cap: the episode prompt only shows 12 lines anyway.
MAX_PER_CLAUSE = 12

_FIELDS = ("obligation_rule", "subject", "action_verb", "action_target", "trigger_condition")
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)\s*```")


def build_batches(
    clauses: list[ExternalClause],
    max_chars: int = MAX_BATCH_CHARS,
    max_clauses: int = MAX_BATCH_CLAUSES,
) -> list[list[ExternalClause]]:
    """Group clauses into batches (a single long clause gets a batch of its own)."""
    batches: list[list[ExternalClause]] = []
    current: list[ExternalClause] = []
    size = 0
    for clause in clauses:
        length = len(clause.full_text)
        if current and (size + length > max_chars or len(current) >= max_clauses):
            batches.append(current)
            current, size = [], 0
        current.append(clause)
        size += length
    if current:
        batches.append(current)
    return batches


def _json_payload(text: str) -> tuple[dict[str, Any], str]:
    """First JSON object in the model output → ``(payload, failure_reason)``.

    Fence-tolerant and tolerant of leading prose; ``failure_reason`` is ``""``
    on success, ``"missing"`` when no ``{`` appears, ``"unparseable"`` when the
    object is cut off or malformed.
    """
    body = text.strip()
    fence = _FENCE_RE.search(body)
    if fence:
        body = fence.group(1)
    start = body.find("{")
    if start < 0:
        return {}, "missing"
    try:
        parsed, _ = json.JSONDecoder().raw_decode(body[start:])
    except json.JSONDecodeError:
        return {}, "unparseable"
    return (parsed, "") if isinstance(parsed, dict) else ({}, "unparseable")


def _clean_obligations(raw: Any) -> list[dict[str, str]]:
    """Normalize to the five string fields; drop rows without a rule or action."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        row = {k: str(item.get(k) or "").strip() for k in _FIELDS}
        if not row["action_verb"] and not row["action_target"]:
            continue
        out.append(row)
        if len(out) >= MAX_PER_CLAUSE:
            break
    return out


def _rows_to_mapping(data: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """``{"clauses": [...]}`` → ``{clause_number: [obligation, ...]}``."""
    rows = data.get("clauses")
    if not isinstance(rows, list):
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        number = str(item.get("clause_number") or "").strip()
        obligations = _clean_obligations(item.get("obligations"))
        if number and obligations:
            out[number] = obligations
    return out


def parse_obligations(text: str) -> dict[str, list[dict[str, str]]]:
    """Model output → ``{clause_number: [obligation, ...]}`` (best effort)."""
    return _rows_to_mapping(_json_payload(text)[0])


def _render_batch(batch: list[ExternalClause]) -> str:
    return "\n\n".join(
        f"【{c.clause_number or '全文'}】\n{c.full_text}" for c in batch
    )


async def _decompose_batch(
    batch: list[ExternalClause],
    llm: Any,
    document: str,
    sem: asyncio.Semaphore,
    depth: int = 0,
) -> dict[str, list[dict[str, str]]]:
    """Extract one batch; an unparsed multi-clause answer is retried as halves."""
    system = render("obligations", document_name=document or "未命名外规")
    try:
        async with sem:
            resp = await llm.ainvoke(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": _render_batch(batch)},
                ],
                max_completion_tokens=OBLIGATION_MAX_TOKENS,
            )
    except Exception as exc:  # noqa: BLE001 — best effort, never fails the stage
        logger.warning("obligation split failed (%d clauses): %s", len(batch), exc)
        return {}

    content = getattr(resp, "content", "") or ""
    data, reason = _json_payload(content)
    if not reason:
        return _rows_to_mapping(data)

    usage = getattr(resp, "usage", {}) or {}
    logger.warning(
        "obligation JSON %s (%d clauses, finish_reason=%s, completion_tokens=%s)"
        " head=%r tail=%r",
        reason, len(batch), getattr(resp, "finish_reason", ""),
        usage.get("completion_tokens"), content[:120], content[-120:],
    )
    if len(batch) <= 1 or depth >= MAX_SPLIT_RETRIES:
        return {}
    mid = len(batch) // 2
    left, right = await asyncio.gather(
        _decompose_batch(batch[:mid], llm, document, sem, depth + 1),
        _decompose_batch(batch[mid:], llm, document, sem, depth + 1),
    )
    return {**left, **right}


async def decompose_obligations(
    clauses: list[ExternalClause], llm: Any, document: str = ""
) -> int:
    """Fill ``clause.obligations`` in place; returns the number extracted.

    Best effort: any failure leaves that batch's clauses with an empty list and
    the comparison prompt then says ``(无拆分义务记录)`` — the chain still runs.
    """
    if llm is None or not clauses:
        return 0
    batches = build_batches(clauses)
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(
        *(_decompose_batch(b, llm, document, sem) for b in batches),
        return_exceptions=True,
    )
    total = 0
    for batch, result in zip(batches, results):
        mapping = result if isinstance(result, dict) else {}
        if not mapping and isinstance(result, BaseException):
            logger.warning("obligation batch errored: %s", result)
        for clause in batch:
            clause.obligations = mapping.get(clause.clause_number, [])
            total += len(clause.obligations)
    logger.info(
        "obligation split: %d obligations over %d clauses in %d llm calls",
        total, len(clauses), len(batches),
    )
    return total
