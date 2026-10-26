"""Clause_Splitter — 条款拆分与内外规比对智能体.

Layering (one-directional)::

    config/models/utils -> llm -> pipeline (4 stages) -> compare
    jobs -> orchestrator (chain) -> the above
    services / api (FastAPI thin shell) on top

Parsing and retrieval go through the external RAG service (``rag``); the ReAct
agent and the chunker stay in-repo.
"""
