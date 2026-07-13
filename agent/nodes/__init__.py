"""Graph nodes."""

from __future__ import annotations

from typing import Any


def get_llm_from_state(state: dict) -> Any:
    """Extract LLM instance from graph state dict.

    Tries state["llm"] first, then state["extra"]["llm"] as fallback.
    """
    llm = state.get("llm")
    if llm is not None:
        return llm
    extra = state.get("extra", {}) or {}
    return extra.get("llm")
