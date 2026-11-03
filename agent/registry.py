"""Tool registry for the ReAct agent: ``knowledge_search`` over the internal corpus.

Search results are truncated once, here, before they reach the model. Every
chunk returned is cached for the lifetime of the registry, so grounding can
resolve the ids the model cites. Create one registry per analysis call.
"""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any

from rag import Chunk, Hit, RagError, RagService
from utils import get_logger

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


_SEARCH_DEF = {
    "type": "function",
    "function": {
        "name": SEARCH_TOOL,
        "description": "Hybrid (semantic + lexical) search over the internal regulation corpus. "
                       "Returns chunk_id, score, snippet and provenance (doc/section).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query."}},
            "required": ["query"],
        },
    },
}

class ToolRegistry:
    """Defines and executes the agent's tools over one RAG service."""

    def __init__(
        self,
        service: RagService,
        *,
        top_k: int = 10,
        snippet_max_tokens: int = 512,
        default_labels: list[str] | None = None,
    ) -> None:
        self.service = service
        self.top_k = top_k
        self.snippet_max_tokens = snippet_max_tokens
        self.default_labels = list(default_labels or [])
        self._chunks: dict[str, Chunk] = {}
        # Searches that raised a retrieval error. The ReAct loop turns tool
        # exceptions into messages, so callers check this to tell "no hits"
        # from "retrieval was down".
        self.failed_searches = 0

    def tool_names(self) -> list[str]:
        return [SEARCH_TOOL]

    def tool_hints(self, lang: str = "zh") -> str:
        return build_tool_hints(self.tool_names(), lang)

    def definitions(self) -> list[dict]:
        return [_SEARCH_DEF]

    # ── retrieval cache ────────────────────────────────────────────────

    def search(self, query: str, k: int | None = None, labels: list[str] | None = None) -> list[Hit]:
        """Run one search and remember the returned chunks.

        ``labels=None`` falls back to ``default_labels``; empty list = no filter.
        """
        available_labels = labels if labels is not None else (self.default_labels or None)
        try:
            hits = self.service.search(query, k or self.top_k, labels=available_labels)
        except RagError:
            self.failed_searches += 1
            raise
        self.remember(hits)
        return hits

    def remember(self, hits: list[Hit]) -> None:
        for hit in hits:
            if hit.chunk_id:
                self._chunks.setdefault(hit.chunk_id, hit.to_chunk())

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        """Resolve a chunk returned earlier in this call (grounding lookup)."""
        return self._chunks.get(chunk_id)

    # ── execution ──────────────────────────────────────────────────────

    def execute(self, name: str, args: dict) -> str:
        """Run ``name`` with ``args`` and return a string for the tool message."""
        if name not in self.tool_names():
            logger.error("unknown tool %r (have: %s)", name, ", ".join(self.tool_names()))
            raise KeyError(f"Unknown tool: {name}")
        t0 = perf_counter()
        result = self._run(name, args)
        logger.info(
            "tool=%s args=%s -> %d bytes in %.1fms",
            name, json.dumps(args, ensure_ascii=False), len(result),
            (perf_counter() - t0) * 1000,
        )
        return result

    def _run(self, name: str, args: dict[str, Any]) -> str:
        hits = self.search(args.get("query", ""))
        return json.dumps(
            [{
                "score": None if h.score is None else round(h.score, 6),
                "chunk_id": h.chunk_id,
                "snippet": truncate_snippet(h.text, self.snippet_max_tokens),
                "meta": h.to_chunk().meta,
            } for h in hits],
            ensure_ascii=False,
        )
