"""Tool registry over the external RAG service.

One ``knowledge_search`` tool (the remote service is hybrid by design), plus
``get_context`` / ``get_materials`` when supported and ``calculate``. Search
results are truncated here, once, before they reach the model — and the full
chunk is cached so grounding can resolve every id the tools returned.
"""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any

from clause_splitter.rag import Chunk, Hit, RagError, RagService
from clause_splitter.utils import get_logger

from .calculator import calculate
from .hints import SEARCH_TOOL, build_tool_hints

logger = get_logger()

_SNIPPET_ATOM = re.compile(r"[一-鿿]|[a-z0-9]+|[^一-鿿a-zA-Z0-9]+", re.IGNORECASE)


def truncate_snippet(text: str, max_tokens: int) -> str:
    """CJK-aware truncation: CJK char ≈ 1 token, latin word ≈ 1 token."""
    if max_tokens <= 0:
        return ""
    count = 0
    cut = len(text)
    for m in _SNIPPET_ATOM.finditer(text):
        seg = m.group()
        if seg[0].isascii():
            cost = 1 if seg[0].isalnum() else 0
        else:
            cost = 1
        if cost and count + cost > max_tokens:
            cut = m.start()
            break
        count += cost
        cut = m.end()
    return text[:cut].rstrip()


def _clamp_scope(scope: list[int], max_span: int) -> list[int]:
    """Normalize a get_context window and cap its span, centered on 0."""
    lo, hi = min(scope), max(scope)
    if hi - lo > max_span:
        half = max_span // 2
        lo, hi = -half, half + (max_span % 2)
    return [max(-max_span, lo), min(max_span, hi)]


class ToolRegistry:
    """Defines and executes the agent's tools over one RAG service."""

    _SEARCH_SCHEMA = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query."}},
        "required": ["query"],
    }

    def __init__(
        self,
        service: RagService,
        *,
        top_k: int = 10,
        snippet_max_tokens: int = 512,
        context_scope_max: int = 8,
    ) -> None:
        self.service = service
        self.top_k = top_k
        self.snippet_max_tokens = snippet_max_tokens
        self.context_scope_max = context_scope_max
        # Full chunks returned during this run (search + get_context) — the
        # grounding authority. Shared by episodes; chunks are immutable.
        self._chunks: dict[str, Chunk] = {}
        self._doc_chunks: dict[str, list[Chunk]] = {}

    # ── tool surface ────────────────────────────────────────────────────

    def tool_names(self) -> list[str]:
        return [SEARCH_TOOL, "get_context", "get_materials", "calculate"]

    def tool_hints(self, lang: str = "zh") -> str:
        return build_tool_hints(self.tool_names(), lang)

    def definitions(self) -> list[dict]:
        defs: list[dict] = [{
            "type": "function",
            "function": {
                "name": SEARCH_TOOL,
                "description": (
                    "Hybrid (semantic + lexical) search over the internal "
                    f"regulation corpus. Returns the top {self.top_k} chunks with "
                    "chunk_id, score, snippet and provenance (doc/section)."
                ),
                "parameters": self._SEARCH_SCHEMA,
            },
        }]
        defs.append({
            "type": "function",
            "function": {
                "name": "get_context",
                "description": (
                    "Return the full text of a chunk window around chunk_id, in "
                    "reading order. scope is an inclusive offset range "
                    "[start, end] relative to the chunk, e.g. [-1, 1]."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chunk_id": {"type": "string"},
                        "scope": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    },
                    "required": ["chunk_id", "scope"],
                },
            },
        })
        defs.append({
            "type": "function",
            "function": {
                "name": "get_materials",
                "description": "List the internal documents available in the corpus.",
                "parameters": {"type": "object", "properties": {}},
            },
        })
        defs.append({
            "type": "function",
            "function": {
                "name": "calculate",
                "description": (
                    "Compute an arithmetic expression and return the exact numeric "
                    "result. Supports + - * / parentheses, **, and "
                    "abs/round/min/max/sum/len/pow/int/float/sqrt/exp/log/"
                    "log2/log10/floor/ceil."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string", "description": "Arithmetic expression."}
                    },
                    "required": ["expression"],
                },
            },
        })
        return defs

    # ── retrieval cache ────────────────────────────────────────────────

    def search(self, query: str, k: int | None = None) -> list[Hit]:
        """Run one search and remember the returned chunks."""
        hits = self.service.search(query, k or self.top_k)
        self.remember(hits)
        return hits

    def remember(self, hits: list[Hit]) -> None:
        for hit in hits:
            if hit.chunk_id:
                self._chunks.setdefault(hit.chunk_id, hit.to_chunk())

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        """Resolve a chunk returned earlier in this run (grounding lookup)."""
        return self._chunks.get(chunk_id)

    def _document_chunks(self, doc_id: str) -> list[Chunk]:
        if doc_id not in self._doc_chunks:
            chunks = sorted(self.service.fetch_chunks(doc_id), key=lambda c: c.order)
            self._doc_chunks[doc_id] = chunks
            for chunk in chunks:
                self._chunks.setdefault(chunk.chunk_id, chunk)
        return self._doc_chunks[doc_id]

    # ── execution ──────────────────────────────────────────────────────

    def execute(self, name: str, args: dict) -> str:
        """Run ``name`` with ``args`` and return a JSON string."""
        t0 = perf_counter()
        result = self._execute(name, args)
        logger.info(
            "tool=%s args=%s -> %d bytes in %.1fms",
            name, json.dumps(args, ensure_ascii=False), len(result),
            (perf_counter() - t0) * 1000,
        )
        return result

    def _execute(self, name: str, args: dict) -> str:
        if name == SEARCH_TOOL:
            hits = self.search(args.get("query", ""))
            return json.dumps(
                [{
                    "score": None if h.score is None else round(h.score, 6),
                    "chunk_id": h.chunk_id,
                    "snippet": truncate_snippet(h.text, self.snippet_max_tokens),
                    "meta": h.to_chunk().with_doc(),
                } for h in hits],
                ensure_ascii=False,
            )
        if name == "get_context":
            chunk_id = args.get("chunk_id")
            if not chunk_id:
                raise ValueError("get_context requires chunk_id")
            anchor = self._chunks.get(chunk_id)
            doc_id = anchor.doc_id if anchor else str(chunk_id).split("#", 1)[0]
            chunks = self._document_chunks(doc_id)
            index = next((i for i, c in enumerate(chunks) if c.chunk_id == chunk_id), None)
            if index is None:
                return json.dumps([], ensure_ascii=False)
            scope = _clamp_scope(args.get("scope", [0, 0]), self.context_scope_max)
            lo = max(0, index + min(scope))
            hi = min(len(chunks) - 1, index + max(scope))
            return json.dumps(
                [{"chunk_id": c.chunk_id, "text": c.text, "meta": c.with_doc()}
                 for c in chunks[lo:hi + 1]],
                ensure_ascii=False,
            )
        if name == "get_materials":
            try:
                return json.dumps(
                    sorted(d.name or d.doc_id for d in self.service.documents()),
                    ensure_ascii=False,
                )
            except RagError as e:
                logger.warning("get_materials failed: %s", e)
                return "[]"
        if name == "calculate":
            return calculate(args.get("expression", ""))
        logger.error("unknown tool %r (have: %s)", name, ", ".join(self.tool_names()))
        raise KeyError(f"Unknown tool: {name}")
