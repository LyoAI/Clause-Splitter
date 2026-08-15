"""拆分结果导出 —— 严格按模板样式生成 Excel.

输出路径: ``{output_dir}/{stem}_条款拆分结果.xlsx``;output_dir 缺省为
``outputs/{stem}/``。同目录附带 ``{stem}_structure.json``(条款级结构,
供比对索引用)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clause_splitter.config import REPO_ROOT, get_settings
from clause_splitter.export.excel_exporter import ExcelExporter
from clause_splitter.models import DocumentStructure
from clause_splitter.utils import get_logger

logger = get_logger()


def _safe_filename(name: str) -> str:
    """把文档名转成安全的文件名/目录名。"""
    import re
    safe = name.strip()
    safe = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", safe)
    safe = re.sub(r"[_\s]+", "_", safe)
    safe = safe.strip("._ ")
    return safe or "未命名文档"


def resolve_output_dir(document_name: str, output_dir: str = "") -> Path:
    """确定导出目录(显式优先,否则 outputs/{stem}/)。"""
    if output_dir:
        return Path(output_dir)
    return Path(REPO_ROOT) / "outputs" / Path(document_name).stem


def export_excel(
    obligations: list[dict[str, Any]],
    document_name: str,
    output_dir: str = "",
    template_path: str = "",
) -> str:
    """把义务记录导出为 Excel(模板样式),返回输出文件绝对路径。

    Args:
        obligations: ClauseObligation dict 列表。
        document_name: 文档名,决定输出文件名。
        output_dir: 输出目录;空则 outputs/{stem}/。
        template_path: 模板路径;空则用 settings.template_path。
    """
    settings = get_settings()
    out_dir = resolve_output_dir(document_name, output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = _safe_filename(Path(document_name).stem) if Path(document_name).suffix else _safe_filename(document_name)
    output_path = out_dir / f"{stem}_条款拆分结果.xlsx"

    try:
        exporter = ExcelExporter(template_path=template_path or settings.template_path)
        exporter.export(obligations, str(output_path), document_name)
    except Exception as e:
        logger.error("Excel 导出失败: %s", e)
        raise

    logger.info("Excel 导出完成: %s (%d 条记录)", output_path, len(obligations))
    return str(output_path)


def write_structure_json(
    doc_structure: DocumentStructure,
    document_name: str,
    output_dir: str = "",
) -> str:
    """把 DocumentStructure 序列化到 ``{output_dir}/{stem}_structure.json``。

    供比对索引(clause_splitter.compare.index)重建条款级 store。
    """
    out_dir = resolve_output_dir(document_name, output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(Path(document_name).stem) if Path(document_name).suffix else _safe_filename(document_name)
    path = out_dir / f"{stem}_structure.json"
    path.write_text(
        json.dumps(doc_structure.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("DocumentStructure 已序列化: %s", path)
    return str(path)
