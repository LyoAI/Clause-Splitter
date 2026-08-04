"""Compare pipeline state — TypedDict for the comparison agent.

Defines the state that flows through the compare pipeline:
    build_index → retrieve_candidates → llm_match → merge_results
"""

from __future__ import annotations

from typing import TypedDict, Any


class CompareState(TypedDict, total=False):
    """State for the comparison pipeline.

    Flows through:
    1. build_index — populate internal_obligations, build bm25 index
    2. batch_compare — split externals into batches, run ReAct agent per batch
    3. merge_results — combine matches, compute statistics
    """

    # Input
    external_obligations: list[dict[str, Any]]
    internal_obligations: list[dict[str, Any]]
    external_document_names: list[str]
    internal_document_names: list[str]
    external_raw_text: str
    internal_raw_text: str

    # Index
    index_path: str
    index_num_docs: int

    # Batch processing
    batch_size: int
    batches: list[list[dict[str, Any]]]
    current_batch_index: int

    # Results
    matches: list[dict[str, Any]]
    total_external: int
    total_internal: int

    # Statistics
    fully_covered: int
    partially_covered: int
    not_covered: int

    # LLM
    llm: Any

    # Control
    status: str
    progress: float
    current_step: str
    error_message: str


def create_compare_state(**kwargs: Any) -> dict[str, Any]:
    """Create initial compare state with defaults."""
    defaults: dict[str, Any] = {
        "external_obligations": [],
        "internal_obligations": [],
        "external_document_names": [],
        "internal_document_names": [],
        "external_raw_text": "",
        "internal_raw_text": "",
        "index_path": "",
        "index_num_docs": 0,
        "batch_size": 15,
        "batches": [],
        "current_batch_index": 0,
        "matches": [],
        "total_external": 0,
        "total_internal": 0,
        "fully_covered": 0,
        "partially_covered": 0,
        "not_covered": 0,
        "llm": None,
        "status": "pending",
        "progress": 0.0,
        "current_step": "",
        "error_message": "",
    }
    defaults.update(kwargs)
    return defaults
