"""Excel 导出节点 —— 确定性步骤.

将已验证的 ClauseObligation 列表导出为 Excel 文件，
严格按模板样式（Baskerville 字体、主题色背景、合并单元格）生成。
"""

from __future__ import annotations

from pathlib import Path

# state is plain dict
from config.settings import get_settings
from utils import get_logger

logger = get_logger()


def export_excel_node(state: dict) -> dict:
    """将拆分结果导出为 Excel 文件.

    Args:
        state: 包含 obligations, document_name 的 AgentState。

    Returns:
        包含 excel_path 及完成状态的部分状态。
    """
    obligations = state.get("obligations", [])
    document_name = state.get("document_name", "unknown")

    settings = get_settings()
    logger.info("正在导出 Excel: %d 条义务记录……", len(obligations))

    # Output path: outputs/{document_name}/
    output_dir = Path(settings.project_root) / "outputs" / Path(document_name).stem
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{Path(document_name).stem}_条款拆分结果.xlsx"

    try:
        from export.excel_exporter import ExcelExporter
        exporter = ExcelExporter(template_path=settings.template_path)
        exporter.export(obligations, str(output_path), document_name)
    except Exception as e:
        logger.error("Excel 导出失败: %s", e)
        return {
            "status": "failed",
            "error_message": f"Excel 导出失败: {e}",
            "current_step": "Excel 导出失败",
        }

    logger.info("Excel 导出完成: %s", output_path)

    return {
        "excel_path": str(output_path),
        "progress": settings.progress_milestones["export_complete"],
        "current_step": "Excel 导出完成",
        "status": "completed",
    }
