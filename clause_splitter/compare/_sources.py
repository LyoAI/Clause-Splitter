"""条款级数据源解析 —— 从拆分产物 JSON 重建条款文本。

三种来源(每份制度):
- ``{stem}_structure.json``      —— 新拆分,含条款 full_text + parent_section。
- ``{stem}_条款拆分结果.json``    —— 新拆分 ClauseSplitResult。
- ``{stem}_split_result.json``   —— 旧拆分 ClauseSplitResult。

统一产出 ``[{doc, clause_number, section, full_text}]`` 列表。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clause_splitter.utils import get_logger

logger = get_logger()


def scan_clause_source_files(internal_dir: Path) -> list[Path]:
    """按优先级收集内规库下的条款来源文件(去重)。

    同一制度的 structure.json 优先于拆分结果 JSON。
    """
    structures = sorted(internal_dir.rglob("*_structure.json"))
    split_results = sorted(internal_dir.rglob("*_split_result.json"))
    split_results += sorted(internal_dir.rglob("*_条款拆分结果.json"))

    seen_stems: set[str] = set()
    ordered: list[Path] = []
    for p in structures + split_results:
        stem = p.name
        if stem in seen_stems:
            continue
        seen_stems.add(stem)
        ordered.append(p)
    return ordered


def clause_from_structure_file(path: Path) -> list[dict[str, Any]]:
    """从 ``{stem}_structure.json`` 读取条款(doc=document_title)。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    doc = str(data.get("document_title") or "").strip() or path.stem.replace("_structure", "")
    clauses: list[dict[str, Any]] = []
    for art in data.get("articles", []):
        full_text = str(art.get("full_text") or "").strip()
        if not full_text:
            continue
        clauses.append({
            "doc": doc,
            "clause_number": str(art.get("article_id") or "").strip() or "全文",
            "section": str(art.get("parent_section") or "").strip() or None,
            "full_text": full_text,
        })
    return clauses


def clause_from_split_result_file(path: Path) -> list[dict[str, Any]]:
    """从拆分结果 JSON 按 clause_number 归并 source_text 重建条款文本。

    兼容 ``_split_result.json`` / ``_条款拆分结果.json`` 两种文件名与
    ``dict`` / ``list`` 两种顶层结构。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        obls = data
        doc = path.stem
    elif isinstance(data, dict):
        obls = data.get("obligations") or []
        doc = str(data.get("document_name") or "").strip() or path.stem
    else:
        return []

    grouped: dict[str, dict[str, Any]] = {}
    for obl in obls:
        if not isinstance(obl, dict):
            continue
        clause = str(obl.get("clause_number") or "").strip() or "全文"
        entry = grouped.setdefault(clause, {"texts": [], "section": None})
        src = str(obl.get("source_text") or "").strip()
        if src and src not in entry["texts"]:
            entry["texts"].append(src)

    clauses: list[dict[str, Any]] = []
    for clause, entry in grouped.items():
        full_text = " ".join(entry["texts"])
        if not full_text.strip():
            continue
        clauses.append({
            "doc": doc,
            "clause_number": clause,
            "section": entry["section"],
            "full_text": full_text,
        })
    return clauses
