"""拆分功能 —— 普通顺序编排,独立于比对."""

from .decompose import decompose_batch
from .export import export_excel, write_structure_json
from .parse import parse_document, parse_excel
from .pipeline import SplitPipeline
from .plan import plan_batches
from .structure import analyze_structure
from .validate import validate, self_correct, validate_with_retry

__all__ = [
    "SplitPipeline",
    "analyze_structure",
    "plan_batches",
    "decompose_batch",
    "validate",
    "self_correct",
    "validate_with_retry",
    "export_excel",
    "write_structure_json",
    "parse_document",
    "parse_excel",
]
