"""Agent tools — retrieval and document access.

- ``<name>_search(query)`` for each bound searcher (grep / bm25 / faiss); each
  returns ``[{score, chunk_id, snippet, meta}]`` — full text is never exposed,
  only a truncated ``snippet``. In ``hybrid`` mode all three search tools are
  bound so the LLM chooses the backend per turn.
- ``get_context(chunk_id, scope)`` — full text window around a chunk, in
  reading order.
- ``get_toc(doc_id)`` — a document's table of contents.
- ``get_materials()`` — the corpus's source document names.
- ``calculate(expression)`` — exact arithmetic.

The registry is the single place that truncates snippets. Searchers emit
full ``text``; ``tools/registry.py`` cuts it before it reaches the LLM.
"""

from .registry import ToolRegistry, build_tool_hints, truncate_snippet
from .searcher import (
    SearcherType,
    build_searchers,
    searcher_kinds,
    Chunk,
    Section,
    DocumentStore,
    Hit,
    BaseSearcher,
)

__all__ = [
    "ToolRegistry",
    "build_tool_hints",
    "truncate_snippet",
    "SearcherType",
    "build_searchers",
    "searcher_kinds",
    "Chunk",
    "Section",
    "DocumentStore",
    "Hit",
    "BaseSearcher",
]
