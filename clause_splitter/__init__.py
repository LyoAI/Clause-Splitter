"""Clause_Splitter — 条款拆分与内外规比对智能体.

Layering (one-directional):

    config.py / models.py / utils  ->  llm  ->  pipeline (4 business stages)
                                                compare (episode / grounding / report)
    jobs -> orchestrator (chain) -> the above
    services / api (FastAPI thin shell) sit on top.

Parsing and retrieval are external. ``parsing`` turns a source document into
blocks (remote parse first, local text-extraction fallback); ``rag`` is the
HTTP client for the corpus service (parse / ingest / search). The ReAct agent
(``agent``) and the per-article chunker (``split/chunker.py``) are algorithm
pieces kept in-repo, not retrieval implementations.
"""
