"""Document preparation (offline): parse → clause structure → chunk → store.

Shared by the internal-library and external-regulation sides, built on the
vendored chillrag parsing primitives (``parse_docs`` / ``chunk_doc`` /
``DocumentStore``). This package only carries the clause-domain structural
enhancements: 编/章/节/条 heading inference (headings) and the Excel
multi-regulation container split (containers).
"""

from .containers import RegulationDoc, split_workbook
from .headings import infer_regulation_headings
from .ingest import ingest, ingest_internal, scan_supported_docs

__all__ = [
    "RegulationDoc",
    "split_workbook",
    "infer_regulation_headings",
    "ingest",
    "ingest_internal",
    "scan_supported_docs",
]
