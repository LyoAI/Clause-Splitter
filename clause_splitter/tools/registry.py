"""Tool registry — the bind-tools layer.

This is where the agent's tool surface is defined. Given the searchers chosen
by ``build_searchers``, the registry exposes one ``<name>_search`` tool per
searcher plus the document-access tool ``get_context``:

- ``hybrid`` configuration = all three search tools bound, so the LLM picks the
  backend per turn. Bind fewer searchers → fewer tools (single-retriever mode).
- **Snippet truncation happens exactly here**, once, for every search tool.
  Searchers return full text; the registry cuts each ``text`` to a ``snippet``
  before it reaches the LLM.

Outputs are JSON strings ready to be fed back to the model.
"""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Callable

from clause_splitter.utils import get_logger
from .searcher import BaseSearcher
from .searcher.store import DocumentStore

logger = get_logger()

# One "atom": a CJK char, a latin/digit word, or a run of other chars
# (whitespace / punctuation). Whitespace and punctuation cost 0 tokens but are
# preserved so the snippet reads naturally.
_SNIPPET_ATOM = re.compile(r"[一-鿿]|[a-z0-9]+|[^一-鿿a-zA-Z0-9]+", re.IGNORECASE)


def truncate_snippet(text: str, max_tokens: int) -> str:
    """Lightweight CJK-aware snippet truncation.

    Counts CJK chars as 1 token each and latin/digit words as 1 token; cuts at a
    token boundary while preserving whitespace. The default tokenizer is
    dependency-free; pass a custom callable to ``ToolRegistry`` to swap it out.
    """
    if max_tokens <= 0:
        return ""
    count = 0
    cut = len(text)
    for m in _SNIPPET_ATOM.finditer(text):
        seg = m.group()
        if seg[0].isascii():
            cost = 1 if seg[0].isalnum() else 0
        else:
            cost = 1  # CJK char
        if cost and count + cost > max_tokens:
            cut = m.start()
            break
        count += cost
        cut = m.end()
    return text[:cut].rstrip()


def _clamp_scope(scope: list[int], max_span: int) -> list[int]:
    """Normalize a get_context window and cap its span, centered on the anchor (0)."""
    lo, hi = min(scope), max(scope)
    if hi - lo > max_span:
        half = max_span // 2
        lo, hi = -half, half + (max_span % 2)
    return [max(-max_span, lo), min(max_span, hi)]


# One-line bilingual hints for the system prompt's ``{tools}`` slot. The API
# schema already carries full descriptions, but models read prompt prose more
# reliably, so each tool gets a terse line here. Keyed by tool name (searchers
# as ``<name>_search``); no workflow is implied — the model picks the tool.
_TOOL_HINTS: dict[str, dict[str, str]] = {
    "grep_search": {
        "zh": "字面关键词搜索，按单个条款关键词查询，优先用内规中出现的词（如“数据分类分级”）",
        "en": "literal keyword search; query with a single clause keyword, preferring terms that appear in the internal regulations (e.g. '数据分类分级')",
    },
    "bm25_search": {
        "zh": "词法 BM25 搜索，适合多词查询（义务主体+动作+事项的组合）",
        "en": "lexical BM25 search, good for multi-word queries (subject+action+target combinations)",
    },
    "faiss_search": {
        "zh": "语义（向量）搜索，适合外规措辞与内规原文不同时的同义/转述检索",
        "en": "semantic (dense vector) search, for paraphrase/synonym phrasing between external and internal wording",
    },
    "get_context": {
        "zh": "读取某条内规条款相邻窗口的完整原文，用于确认搜索片段上下文是否足够判断承接关系",
        "en": "return the full clause text around a chunk_id window to confirm whether the snippet context suffices for a coverage judgment",
    },
}

_FIXED_TOOLS = ("get_context",)


class ToolRegistry:
    """Defines and executes the agent's tool set over a searcher set + store."""

    #: JSON schema for the shared search-tool arguments.
    _SEARCH_SCHEMA = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query."}},
        "required": ["query"],
    }

    def __init__(
        self,
        searchers: dict[str, BaseSearcher],
        store: DocumentStore,
        top_k: int = 10,
        snippet_max_tokens: int = 512,
        context_scope_max: int = 8,
        truncate: Callable[[str, int], str] = truncate_snippet,
    ) -> None:
        self.searchers = searchers
        self.store = store
        self.top_k = top_k
        self.snippet_max_tokens = snippet_max_tokens
        self.context_scope_max = context_scope_max
        self.truncate = truncate

    # ------------------------------------------------------------- schema --

    def definitions(self) -> list[dict]:
        """OpenAI function-calling tool definitions for this registry."""
        defs = []
        for name, searcher in self.searchers.items():
            defs.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"{name}_search",
                        "description": searcher.get_description(self.top_k),
                        "parameters": self._SEARCH_SCHEMA,
                    },
                }
            )
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": "get_context",
                    "description": (
                        "Return the full text of a chunk window around chunk_id, in "
                        "reading order. scope is an inclusive offset range "
                        "[start, end] relative to the chunk, e.g. [-1, 1] = previous "
                        "clause + this clause + next clause. Use this when a search "
                        "snippet is too short to judge whether an internal clause "
                        "covers an external requirement."
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
            }
        )
        return defs

    def tool_names(self) -> list[str]:
        """Names of every tool this registry exposes."""
        return [d["function"]["name"] for d in self.definitions()]

    def tool_hints(self, lang: str = "zh") -> str:
        """Bulleted ``- name: hint`` tool list for the system prompt's ``{tools}`` slot.

        One terse line per tool in the prompt's language, so the model sees what
        each tool does without having to read the API function schema. No
        workflow is prescribed — choosing and ordering the calls is up to the agent.
        """
        sep = "：" if lang == "zh" else ": "
        rows = [
            f"- {name}_search{sep}{_TOOL_HINTS[name + '_search'][lang]}"
            for name in self.searchers
        ]
        rows += [f"- {name}{sep}{_TOOL_HINTS[name][lang]}" for name in _FIXED_TOOLS]
        return "\n".join(rows)

    # ------------------------------------------------------------ execute --

    def execute(self, name: str, args: dict) -> str:
        """Run ``name`` with ``args`` and return a JSON string result."""
        t0 = perf_counter()
        result = self._execute(name, args)
        logger.info("tool=%s args=%s -> %d bytes in %.1fms",
                    name, json.dumps(args, ensure_ascii=False), len(result), (perf_counter() - t0) * 1000)
        return result

    def _execute(self, name: str, args: dict) -> str:
        if name.endswith("_search"):
            searcher = self.searchers.get(name[: -len("_search")])
            if searcher is None:
                raise KeyError(f"Unknown search tool: {name}")
            query = args.get("query", "")
            hits = searcher.search(query, self.top_k)
            return json.dumps(
                [
                    {
                        "score": round(h.score, 6),
                        "chunk_id": h.chunk_id,
                        "snippet": self.truncate(h.text, self.snippet_max_tokens),
                        "meta": h.meta,
                    }
                    for h in hits
                ],
                ensure_ascii=False,
            )
        if name == "get_context":
            chunk_id = args.get("chunk_id")
            if not chunk_id:
                raise ValueError("get_context requires chunk_id")
            scope = _clamp_scope(args.get("scope", [0, 0]), self.context_scope_max)
            chunks = self.store.get_context(chunk_id, scope)
            return json.dumps(
                [
                    {"chunk_id": c.chunk_id, "text": c.text, "meta": c.with_doc()}
                    for c in chunks
                ],
                ensure_ascii=False,
            )
        raise KeyError(f"Unknown tool: {name}")
