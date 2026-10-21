"""拆分功能 —— 结构分析、义务拆解与文档解析."""

from .decompose import decompose_batch
from .plan import plan_batches
from .structure import analyze_structure

__all__ = [
    "analyze_structure",
    "plan_batches",
    "decompose_batch",
]
