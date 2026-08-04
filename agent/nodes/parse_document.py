"""文档解析节点 —— 确定性步骤.

从上传的文件（PDF/DOCX/DOC/TXT/XLSX/XLS）提取纯文本，存入 state.raw_text。

对于 Excel 文件（.xlsx/.xls），本节点仅做文件类型标记，
实际的结构化解析由 parse_excel 节点完成（ReAct 模式）。
"""

from __future__ import annotations

from pathlib import Path

from config.settings import get_settings
from utils import get_logger
from utils.file.doc_parser import extract_text

logger = get_logger()


def parse_document_node(state: dict) -> dict:
    """解析上传的文档，提取纯文本。

    对于 PDF/DOCX/DOC/TXT：直接提取纯文本。
    对于 XLSX/XLS：仅做文件类型标记，由 parse_excel 节点进一步处理。

    Args:
        state: 包含 file_path 和 document_name 的 AgentState。

    Returns:
        包含 raw_text（或空）、file_type 及进度更新的部分状态。
    """
    file_path = state.get("file_path", "")
    document_name = state.get("document_name", "")

    if not file_path:
        return {
            "raw_text": "",
            "file_type": "",
            "status": "failed",
            "error_message": "file_path 为空，无法解析文档",
            "current_step": "文档解析失败",
        }

    settings = get_settings()
    suffix = Path(file_path).suffix.lower()
    logger.info("正在解析文档: %s (%s) [%s]", document_name, file_path, suffix)

    # ── Excel path: defer to parse_excel node ──────────────────────────
    if suffix in (".xlsx", ".xls"):
        logger.info("检测到 Excel 文件，路由至 Excel 解析管线: %s", document_name)
        return {
            "raw_text": "",  # Will be set by parse_excel
            "file_type": "excel",
            "progress": settings.progress_milestones["parse_complete"],
            "current_step": "Excel 文件已识别，准备结构化解析……",
            "status": "parsing",
        }

    # ── Text-based documents (PDF/DOCX/DOC/TXT) ────────────────────────
    try:
        raw_text = extract_text(file_path, max_chars=settings.max_chars_per_document)
    except Exception as e:
        logger.error("文档解析失败: %s", e)
        return {
            "raw_text": "",
            "file_type": "text",
            "status": "failed",
            "error_message": f"文档解析失败: {e}",
            "current_step": "文档解析失败",
        }

    if not raw_text or len(raw_text.strip()) < 10:
        return {
            "raw_text": raw_text,
            "file_type": "text",
            "status": "failed",
            "error_message": "文档内容为空或过短，无法分析",
            "current_step": "文档解析失败 —— 内容为空",
        }

    logger.info("文档解析完成，共 %d 字符", len(raw_text))

    return {
        "raw_text": raw_text,
        "file_type": "text",
        "progress": settings.progress_milestones["parse_complete"],
        "current_step": "文档解析完成",
        "status": "parsing",
    }
