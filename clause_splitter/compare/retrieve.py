"""Deterministic batch pre-retrieval for the HTML 复核展示 page.

For every external clause we additionally run one remote search and surface
the top-k internal clauses the recall relates to — display-only evidence the
作业人员 judges against the episode's own result. The batch query is the clause
text itself; the batch result never feeds back into the episode (no LLM, no
state, no score contamination). Duplicate chunk ids keep their best score.
"""

from __future__ import annotations

import html
import re

from clause_splitter.config import REVIEW_TOP_K
from clause_splitter.models import RetrievedCandidate
from clause_splitter.rag import RagError, RagService
from clause_splitter.utils import get_logger
from .ground import _segment_label

logger = get_logger()

_KEYWORD_RE = re.compile(r"[一-鿿]{2,}|[A-Za-z0-9]{3,}")


def keywords(text: str) -> list[str]:
    """Meaningful query keywords: ≥2-char CJK runs and ≥3-char ASCII words."""
    if not text:
        return []
    return list(dict.fromkeys(_KEYWORD_RE.findall(text)))


def _snippet(text: str, query: str, width: int = 180) -> str:
    """A compact window around the first query-keyword hit in ``text``."""
    text = " ".join(text.split())
    pos = -1
    for kw in keywords(query):
        i = text.find(kw)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        return text[:width]
    start = max(0, pos - 60)
    prefix = "…" if start > 0 else ""
    return prefix + text[start:start + width]


def _match_spans(text: str, kws: list[str]) -> list[tuple[int, int]]:
    """Every verbatim occurrence of each keyword, in reading order."""
    spans: list[tuple[int, int]] = []
    for kw in kws:
        start = 0
        while True:
            i = text.find(kw, start)
            if i == -1:
                break
            spans.append((i, i + len(kw)))
            start = i + len(kw)
    return sorted(spans)


def _fallback_shared_spans(text: str, kws: list[str], max_len: int = 10) -> list[tuple[int, int]]:
    """Longest shared substrings between ``text`` and the keywords, greedy.

    Dense legal clauses rarely appear *verbatim* in a snippet, so when nothing
    matches exactly we highlight the longest lexical fragments the snippet still
    shares with the query. Candidates are substrings (len 2..max_len) of each
    keyword, tried longest-first; an accepted span blocks overlapping ones so the
    page stays readable. ``max_len`` caps the fragment size — a 10-char fragment
    appearing verbatim is already strong evidence of relevance.
    """
    cands: set[str] = set()
    for kw in kws:
        n = len(kw)
        for size in range(min(n, max_len), 1, -1):
            for i in range(n - size + 1):
                cands.add(kw[i:i + size])
    ordered = sorted(cands, key=lambda c: (-len(c), c))  # longest, then lexicographic → deterministic
    spans: list[tuple[int, int]] = []
    for cand in ordered:
        start = 0
        while True:
            i = text.find(cand, start)
            if i == -1:
                break
            if all(i >= e or s >= i + len(cand) for s, e in spans):
                spans.append((i, i + len(cand)))
                break
            start = i + 1
    return sorted(spans)


def _render_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Merge overlapping spans and emit HTML-escaped text with ``<mark>``."""
    merged = [spans[0]]
    for s in spans[1:]:
        if s[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], s[1]))
        else:
            merged.append(s)
    out: list[str] = []
    prev = 0
    for a, b in merged:
        out.append(html.escape(text[prev:a]))
        out.append("<mark>")
        out.append(html.escape(text[a:b]))
        out.append("</mark>")
        prev = b
    out.append(html.escape(text[prev:]))
    return "".join(out)


def highlight(text: str, query: str) -> str:
    """HTML-escape ``text`` and wrap query-keyword matches in ``<mark>``.

    Verbatim keyword matches first; when none match, falls back to longest
    shared substrings (see ``_fallback_shared_spans``). Always escapes user text.
    """
    kws = keywords(query)
    if not kws:
        return html.escape(text)
    spans = _match_spans(text, kws)
    if not spans:
        spans = _fallback_shared_spans(text, kws)
    if not spans:
        return html.escape(text)
    return _render_spans(text, spans)


def pre_retrieve(
    service: RagService,
    clause_text: str,
    *,
    top_k: int = REVIEW_TOP_K,
) -> list[RetrievedCandidate]:
    """Top-k internal chunks for ``clause_text`` from the RAG service.

    Args:
        service: External retrieval service.
        clause_text: The external clause text — used verbatim as the query.
        top_k: How many candidates to keep, best score first.

    Returns:
        Candidates, highest score first; a failed search is logged and yields
        no candidates rather than failing the batch.
    """
    if not clause_text:
        return []
    try:
        hits = service.search(clause_text, top_k)
    except RagError as e:
        logger.warning("pre_retrieve 检索失败,本条无候选: %s", e)
        return []

    best: dict[str, dict] = {}
    for h in hits:
        if not h.chunk_id:
            continue
        entry = best.get(h.chunk_id)
        if entry is None:
            best[h.chunk_id] = {"score": h.score, "hit": h}
        elif h.score is not None and (entry["score"] is None or h.score > entry["score"]):
            entry["score"] = h.score

    def _key(entry: dict) -> tuple[float, str]:
        score = entry["score"]
        return (-(score if score is not None else float("-inf")), entry["hit"].chunk_id)

    candidates: list[RetrievedCandidate] = []
    for entry in sorted(best.values(), key=_key)[:top_k]:
        h = entry["hit"]
        chunk = h.to_chunk()
        doc_name = h.doc_id or str(h.meta.get("doc") or h.chunk_id.split("#", 1)[0])
        candidates.append(
            RetrievedCandidate(
                chunk_id=h.chunk_id,
                doc_name=doc_name,
                clause_label=_segment_label(chunk),
                backend=service.name,
                score=entry["score"],
                query=clause_text,
                snippet=_snippet(h.text, clause_text),
                full_text=h.text,
            )
        )
    return candidates
