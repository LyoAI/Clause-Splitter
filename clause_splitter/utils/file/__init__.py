"""文件工具 —— 文档解析 + JSON 提取."""

from .doc_parser import clear_cache, extract_text
from .json_utils import parse_json_from_text, safe_get

__all__ = ["extract_text", "clear_cache", "parse_json_from_text", "safe_get"]
