"""外规条款装载 —— 目录 / JSON / 文本文档三种来源 → ExternalClause 列表.

供 ``run_compare.py`` 与 FastAPI 薄壳共用,避免根脚本依赖。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from clause_splitter.utils import get_logger
from ._sources import (
    clause_from_split_result_file,
    clause_from_structure_file,
    scan_clause_source_files,
)
from .episode import ExternalClause

logger = get_logger()


def _clean_doc_name(name: str) -> str:
    """去掉制度名里的文件后缀(旧拆分结果把文件名存成 document_name)。"""
    for suffix in (".docx", ".doc", ".pdf", ".txt", ".xlsx", ".xls"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def _obligations_by_clause(path: Path) -> dict[str, list[dict]]:
    """从拆分结果 JSON 按 clause_number 归并义务 dict(供 episode 参考)。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        obls = data
    elif isinstance(data, dict):
        obls = data.get("obligations") or []
    else:
        return {}
    grouped: dict[str, list[dict]] = {}
    for obl in obls:
        if not isinstance(obl, dict):
            continue
        clause = str(obl.get("clause_number") or "").strip() or "全文"
        grouped.setdefault(clause, []).append(obl)
    return grouped


def external_clauses_from_file(path: Path) -> list[ExternalClause]:
    """从一份 structure.json / 拆分结果 JSON 装载外规条款。"""
    name = path.name
    obligations_by_clause: dict[str, list[dict]] = {}
    if name.endswith("_structure.json"):
        clause_texts = clause_from_structure_file(path)
    else:
        clause_texts = clause_from_split_result_file(path)
        obligations_by_clause = _obligations_by_clause(path)

    clauses: list[ExternalClause] = []
    for c in clause_texts:
        clause_number = str(c["clause_number"] or "").strip() or "全文"
        clauses.append(ExternalClause(
            document_name=_clean_doc_name(str(c["doc"] or "").strip()),
            clause_number=clause_number,
            section=str(c.get("section") or "").strip(),
            full_text=str(c.get("full_text") or "").strip(),
            obligations=obligations_by_clause.get(clause_number, []),
        ))
    return clauses


def load_external_clauses(external: str, output_dir: str, llm) -> list[ExternalClause]:
    """装载外规条款列表(目录 / JSON / 文本文档三种来源)。

    Args:
        external: 目录(扫描全部条款来源) / ``*_structure.json`` /
            拆分结果 JSON / docx/pdf/txt(自动先拆分)。
        output_dir: 自动拆分时的产物目录。
        llm: ``LLM`` facade(自动拆分用)。
    """
    path = Path(external)
    if path.is_dir():
        clauses: list[ExternalClause] = []
        for f in scan_clause_source_files(path):
            clauses.extend(external_clauses_from_file(f))
        return clauses

    if path.suffix.lower() == ".json":
        clauses = external_clauses_from_file(path)
        if clauses:
            return clauses
        raise SystemExit(f"无法从 JSON 读取外规条款: {path}")

    # docx/pdf/txt → 自动拆分,再读 structure.json。
    from clause_splitter.split import SplitPipeline

    out_dir = Path(output_dir) if output_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    pipeline = SplitPipeline(llm=llm)

    def _progress(p: float, step: str, _state: dict) -> None:
        print(f"[{p:5.1f}%] {step}")

    print(f"═══ 外规自动拆分: {path.name} ═══")
    result = asyncio.run(
        pipeline.run(path, document_name=path.name, output_dir=str(out_dir), progress=_progress)
    )
    print(f"拆分完成: {result.total_articles} 条款 → {result.total_obligations} 义务\n")

    # 定位 pipeline 写出的 structure.json。
    stem = path.stem
    structure = out_dir / f"{stem}_structure.json"
    if not structure.exists():
        candidates = sorted(out_dir.glob(f"{stem}*structure.json")) + sorted(out_dir.rglob("*_structure.json"))
        structure = candidates[0] if candidates else None
    if structure is None:
        raise SystemExit(f"自动拆分后未找到 structure.json(输出目录: {out_dir})")

    return external_clauses_from_file(structure)
