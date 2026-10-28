"""Per-job wiring shared by the pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from clause_splitter.db import (
    ClauseRepo,
    InternalDocRepo,
    JobRepo,
    RegulationRepo,
    default_repos as _build_repos,
)
from clause_splitter.storage import regulation_dir

#: Stage names, in execution order.
STAGES = ("applicability", "split", "internalization", "analysis")

#: Push ``docKind`` values that go to the corpus instead of the external chain:
#: internal rules and attachments are ingested (Zeus parses/chunks/embeds them)
#: and split locally, but never judged for applicability or internalized.
INTERNAL_KINDS = ("internal", "attach")


def is_internal(reg: dict[str, Any]) -> bool:
    """Whether this regulation came from an internal/attachment push."""
    return (reg.get("push_doc_kind") or "") in INTERNAL_KINDS


#: Derived fields cleared when a stage is (re-)run or skipped — keeps
#: regulations free of stale data from a previous generation.
DERIVED_FIELDS: dict[str, dict[str, Any]] = {
    "applicability": {
        "applicability_applicable": None,
        "applicability_reason": "",
        "applicability_confidence": 0.0,
        "applicability_truncated": False,
    },
    "split": {
        "article_count": None,
        "split_mode": "",
        "doc_abstract": "",
        "doc_keyword": "",
        "doc_profile_state": "",
    },
    "internalization": {"needs_internalization_count": None, "no_internalization_count": None},
    "analysis": {"analysis_summary": None, "analysis_index_signature": ""},
}

#: Downstream stages reset when a stage re-runs.
DOWNSTREAM: dict[str, tuple[str, ...]] = {
    "applicability": ("split", "internalization", "analysis"),
    "split": ("internalization", "analysis"),
    "internalization": ("analysis",),
    "analysis": (),
}


class StageError(RuntimeError):
    """A stage failed with a user-facing message (job → failed)."""


@dataclass
class Repos:
    regulations: RegulationRepo
    clauses: ClauseRepo
    jobs: JobRepo
    internal_docs: InternalDocRepo


def default_repos() -> Repos:
    regs, clauses, jobs, docs = _build_repos()
    return Repos(regulations=regs, clauses=clauses, jobs=jobs, internal_docs=docs)


@dataclass
class StageContext:
    """Everything one stage execution needs; built per job by the runner.

    ``progress`` reports the stage-local fraction (0.0–1.0) plus a step label;
    the runner scales it into job progress. ``rag`` is the external retrieval
    service (parse / search / corpus inspection).
    """

    regulation: dict[str, Any]
    llm: Any
    repos: Repos
    payload: dict[str, Any] = field(default_factory=dict)
    progress: Callable[[float, str], None] = field(default=lambda p, s: None)
    rag: Any = None

    @property
    def reg_id(self) -> str:
        return self.regulation["_id"]

    @property
    def reg_dir(self) -> Path:
        return regulation_dir(self.reg_id)
