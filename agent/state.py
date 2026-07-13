"""Agent state — TypedDict with merge strategies for LangGraph."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict
from operator import add


class AgentState(TypedDict, total=False):
    """Global state for the clause-splitting pipeline.

    Each key uses an Annotated reducer:
    - No annotation = last-write-wins (replace on update)
    - `add` = append (for lists like messages)
    """

    # Input
    file_path: str
    document_name: str

    # Parsing
    raw_text: str

    # Structure analysis
    document_structure: dict | None
    total_articles: int

    # Batch planning
    batches: list
    current_batch_index: int

    # Decomposition results
    obligations: list
    processed_articles: int

    # Validation
    validation_errors: list
    retry_count: int

    # Export
    excel_path: str | None

    # Task control
    status: str
    progress: float
    current_step: str
    error_message: str | None

    # Extra (LLM instance, etc.)
    llm: Any


def create_initial_state(**kwargs: Any) -> dict:
    """Create initial state with all required defaults."""
    defaults: dict[str, Any] = {
        "file_path": "",
        "document_name": "",
        "raw_text": "",
        "document_structure": None,
        "total_articles": 0,
        "batches": [],
        "current_batch_index": 0,
        "obligations": [],
        "processed_articles": 0,
        "validation_errors": [],
        "retry_count": 0,
        "excel_path": None,
        "status": "pending",
        "progress": 0.0,
        "current_step": "",
        "error_message": None,
        "llm": None,
    }
    defaults.update(kwargs)
    return defaults
