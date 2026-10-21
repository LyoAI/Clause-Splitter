"""chillrag — local document retrieval and question answering service.

Self-contained replacement for a hosted dense-retrieval service: parse and
index a folder of documents, then answer two questions a retrieval caller
asks — *which passages relate to this query* (``RAGTool.search_by_question``)
and *what is the answer to this query* (``RAGTool.answer_by_question``).

General by design: the corpus is any set of MinerU-parseable documents and the
queries are arbitrary natural language. Nothing here assumes a domain — a
caller's retrieval policy (what to filter, how to phrase, what output format)
is passed in, never baked in.
"""

__all__ = ["RAGTool", "CorpusManager"]


def __getattr__(name: str):
    """Lazy re-exports — importing the package must not load faiss / torch."""
    if name == "RAGTool":
        from .rag_tool import RAGTool
        return RAGTool
    if name == "CorpusManager":
        from .corpus import CorpusManager
        return CorpusManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
