"""文档解析服务 —— 封装文档格式检测与文本提取."""

from __future__ import annotations

from pathlib import Path

from utils import get_logger
from utils.file.doc_parser import extract_text

logger = get_logger()

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


class DocumentParseService:
    """文档解析服务.

    负责：
    1. 文件格式验证
    2. 文本提取
    3. 基础统计（字符数、行数等）
    """

    @staticmethod
    def validate_file(file_path: str | Path, file_size: int = 0) -> dict:
        """验证上传文件是否合法.

        Args:
            file_path: 文件路径。
            file_size: 文件大小（字节）。

        Returns:
            {"valid": bool, "error": str | None}
        """
        path = Path(file_path)
        suffix = path.suffix.lower()

        if suffix not in ALLOWED_EXTENSIONS:
            return {
                "valid": False,
                "error": f"不支持的文件格式: {suffix}。支持的格式: {', '.join(ALLOWED_EXTENSIONS)}",
            }

        if file_size > MAX_FILE_SIZE:
            return {
                "valid": False,
                "error": f"文件过大: {file_size / 1024 / 1024:.1f} MB。最大允许: {MAX_FILE_SIZE / 1024 / 1024:.0f} MB",
            }

        if file_size == 0:
            return {
                "valid": False,
                "error": "文件为空",
            }

        return {"valid": True, "error": None}

    @staticmethod
    def parse(file_path: str | Path, max_chars: int = 200000) -> dict:
        """解析文档，提取文本和基础统计.

        Args:
            file_path: 文件路径。
            max_chars: 最大提取字符数。

        Returns:
            {
                "success": bool,
                "text": str,
                "char_count": int,
                "line_count": int,
                "error": str | None,
            }
        """
        path = Path(file_path)

        if not path.exists():
            return {
                "success": False,
                "text": "",
                "char_count": 0,
                "line_count": 0,
                "error": f"文件不存在: {file_path}",
            }

        try:
            text = extract_text(str(path), max_chars=max_chars)
        except Exception as e:
            logger.error("文档解析异常: %s", e)
            return {
                "success": False,
                "text": "",
                "char_count": 0,
                "line_count": 0,
                "error": f"文档解析失败: {e}",
            }

        if not text or len(text.strip()) < 10:
            return {
                "success": False,
                "text": text,
                "char_count": len(text),
                "line_count": 0,
                "error": "文档内容为空或过短，无法分析",
            }

        lines = [l for l in text.splitlines() if l.strip()]
        logger.info("文档解析成功: %d 字符, %d 非空行", len(text), len(lines))

        return {
            "success": True,
            "text": text,
            "char_count": len(text),
            "line_count": len(lines),
            "error": None,
        }
