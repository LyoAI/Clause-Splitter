"""Clause_Splitter — 条款解构 + 内外规比对智能体。

Layering (one-directional):
    config.py / models / utils  →  llm  →  tools  →  agent
                                                →  split / compare
    api (FastAPI thin shell) sits on top of the CLI-facing services.
"""

__version__ = "0.2.0"
