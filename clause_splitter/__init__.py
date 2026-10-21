"""Clause_Splitter — 条款拆分与内外规比对智能体.

Layering (one-directional):

    config.py / models.py / utils  ->  llm  ->  pipeline (4 business stages)
                                                compare (ingest / episode / grounding / report)
    jobs -> orchestrator (engine seam + chain) -> the above
    services / api (FastAPI thin shell) sit on top.

The RAG engine is the vendored ``chillrag`` package (general-purpose, no
domain assumptions): at runtime only ``orchestrator`` and the compare.ingest /
compare.index preparation modules import it — retrieval policy and prompts
are passed in as injected arguments (engine seam).
"""
