"""Tool registry — the bind-tools layer.

This is where the agent's tool surface is defined. Given the searchers chosen
by ``build_searchers``, the registry exposes one ``<name>_search`` tool per
searcher plus the document-access tools ``get_context``, ``get_toc``,
``get_materials``, and the arithmetic tool ``calculate``:

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
from typing import Callable, Iterable

from chillrag.utils import get_logger
from .calculator import calculate
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
        "zh": "字面关键词搜索，按单个关键词查询，优先用材料中出现的词",
        "en": "literal keyword search; query with a single keyword, preferring terms that appear in the materials",
    },
    "bm25_search": {
        "zh": "词法 BM25 搜索，适合多词查询",
        "en": "lexical BM25 search, good for multi-word queries",
    },
    "faiss_search": {
        "zh": "语义（向量）搜索，适合问法与原文措辞不同时",
        "en": "semantic (dense vector) search, for paraphrase/synonym phrasing",
    },
    "get_context": {
        "zh": "读取 chunk 附近窗口的完整文本，用于确认搜索片段是否足够",
        "en": "return the full text of a chunk window around chunk_id",
    },
    "get_toc": {
        "zh": "返回文档目录（章节标题、层级、页码）；适用于有章节结构的文档（PDF 等），表格文件无目录",
        "en": "return a document's table of contents (sections, levels, pages); for heading-structured documents (PDFs, ...) — spreadsheets have none",
    },
    "get_materials": {
        "zh": "列出语料中可用的源文档文件名，先调用了解有哪些材料",
        "en": "list the source documents available in the corpus; call it first to see what materials exist",
    },
    "calculate": {
        "zh": "计算算术表达式并返回精确数值；任何计算都用它，不要手算",
        "en": "evaluate an arithmetic expression and return the exact result; use it for any computation instead of doing the arithmetic by hand",
    },
}

_FIXED_TOOLS = ("get_context", "get_toc", "get_materials", "calculate")


def build_tool_hints(searcher_names: Iterable[str], lang: str = "zh") -> str:
    """Bulleted ``- name: hint`` tool list in ``lang`` for the ``{tools}`` slot.

    Module-level so a caller can assemble a system prompt before any registry
    exists: the searcher names are a pure function of the searcher config (the
    keys ``build_searchers`` produces) and the hint texts are the bilingual
    ``_TOOL_HINTS`` above. ``ToolRegistry.tool_hints`` wraps this.
    """
    sep = "：" if lang == "zh" else ": "
    rows = [
        f"- {name}_search{sep}{_TOOL_HINTS[name + '_search'][lang]}"
        for name in searcher_names
    ]
    rows += [f"- {name}{sep}{_TOOL_HINTS[name][lang]}" for name in _FIXED_TOOLS]
    return "\n".join(rows)


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
                        "chunk + this chunk + next chunk. Use this when a search "
                        "snippet is too short to answer confidently."
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
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": "get_toc",
                    "description": (
                        "Return the table of contents of a document: section titles, "
                        "heading level, and page. Use it to navigate a "
                        "heading-structured document (e.g. a PDF report) before "
                        "reading chunks. Documents without headings (spreadsheets, "
                        "plain text) have no TOC — returns an empty list."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"doc_id": {"type": "string"}},
                        "required": ["doc_id"],
                    },
                },
            }
        )
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": "get_materials",
                    "description": (
                        "List the source documents available in this corpus (their "
                        "file names). Call it first to learn what documents exist "
                        "before navigating with get_toc or searching."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        )
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": "calculate",
                    "description": (
                        "Compute an arithmetic expression and return the exact numeric "
                        "result. Use it for any computation (ratio, growth rate, sum "
                        "of items, ...) instead of calculating by hand: write the "
                        "formula with concrete numbers and let this tool evaluate it. "
                        "Supports + - * / parentheses, ** (power), and "
                        "abs/round/min/max/sum/len/pow/int/float/sqrt/exp/log/"
                        "log2/log10/floor/ceil; commas inside numbers are "
                        "stripped (1,234.5 -> 1234.5). Example: "
                        "calculate('(14307609 + 33246902) / 576394573 * 100')"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "expression": {"type": "string", "description": "Arithmetic expression."}
                        },
                        "required": ["expression"],
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
        return build_tool_hints(self.searchers, lang)

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
                logger.error("unknown search tool %r (have: %s)", name, ", ".join(self.searchers))
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
                logger.error("get_context called without chunk_id")
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
        if name == "get_toc":
            doc_id = args.get("doc_id")
            if not doc_id:
                logger.error("get_toc called without doc_id")
                raise ValueError("get_toc requires doc_id")
            sections = self.store.get_toc(doc_id)
            return json.dumps(
                [
                    {
                        "section_id": s.section_id,
                        "title": s.title,
                        "level": s.level,
                        "page": s.page,
                    }
                    for s in sections
                ],
                ensure_ascii=False,
            )
        if name == "get_materials":
            return json.dumps(sorted(self.store.docs.keys()), ensure_ascii=False)
        if name == "calculate":
            return calculate(args.get("expression", ""))
        logger.error("unknown tool %r (have: %s)", name, ", ".join(self.tool_names()))
        raise KeyError(f"Unknown tool: {name}")
