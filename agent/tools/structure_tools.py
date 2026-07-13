"""文档结构识别工具 —— 用于 analyze_structure 节点（ReAct）.

提供灵活的法律文档结构识别能力，不局限于固定正则模式。
"""

from __future__ import annotations

import re
from typing import Any

from utils import get_logger

logger = get_logger()


# ── 工具: 正则分段 ─────────────────────────────────────────────────────────

def segment_by_regex(text: str, pattern: str) -> dict[str, Any]:
    """使用正则表达式对文本进行分段.

    尝试给定的正则模式来匹配文档中的章节/条款边界。
    常用于 "第X条"、"第X章"、"Article X" 等标准编号模式。

    Args:
        text: 要分段的完整文本。
        pattern: Python 正则表达式，需包含一个捕获组用于提取编号。

    Returns:
        {
            "success": true/false,
            "total_segments": 分段数量,
            "segments": [{id, start, end, text_preview}, ...],
            "coverage_ratio": 匹配覆盖的字符比例,
            "message": 说明信息
        }
    """
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return {
            "success": False,
            "total_segments": 0,
            "segments": [],
            "coverage_ratio": 0.0,
            "message": f"无效的正则表达式: {e}",
        }

    matches = list(compiled.finditer(text))
    if not matches:
        return {
            "success": False,
            "total_segments": 0,
            "segments": [],
            "coverage_ratio": 0.0,
            "message": f"模式 '{pattern}' 未匹配到任何内容。请尝试其他模式，如 r'第[一二三四五六七八九十百零\\\\d]+条'",
        }

    segments: list[dict[str, Any]] = []
    for i, match in enumerate(matches):
        article_id = match.group(1) if match.lastindex else match.group(0)
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg_text = text[start:end].strip()
        segments.append({
            "id": article_id,
            "start": start,
            "end": end,
            "char_count": end - start,
            "text_preview": seg_text[:150] + ("..." if len(seg_text) > 150 else ""),
        })

    # 计算覆盖比例
    first_start = matches[0].start()
    last_end = matches[-1].start()
    total_len = len(text)
    coverage = (last_end - first_start) / max(total_len, 1)

    return {
        "success": True,
        "total_segments": len(segments),
        "segments": segments,
        "coverage_ratio": round(coverage, 3),
        "message": f"成功用模式 '{pattern}' 匹配到 {len(segments)} 个段落，覆盖率 {coverage:.1%}",
    }


# ── 工具: 章节识别 ─────────────────────────────────────────────────────────

def identify_chapters(text: str) -> dict[str, Any]:
    """Identify chapter/section structure in a document.

    Uses configurable regex patterns from settings. Tries each pattern
    in order; returns the first one that matches at least 1 occurrence.

    Args:
        text: Full document text.

    Returns:
        {"chapters": [...], "pattern_used": str, "total_chapters": int}
    """
    from config.settings import get_settings
    settings = get_settings()

    for pattern, name in settings.chapter_patterns:
        matches = list(re.finditer(pattern, text))
        if len(matches) >= 1:
            chapters = []
            for i, match in enumerate(matches):
                title = match.group(0).strip()
                start = match.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                chapters.append({
                    "title": title,
                    "start": start,
                    "end": end,
                })
            return {
                "chapters": chapters,
                "pattern_used": name,
                "total_chapters": len(chapters),
            }

    return {
        "chapters": [],
        "pattern_used": "none",
        "total_chapters": 0,
    }


# ── 工具: 提取条款文本 ─────────────────────────────────────────────────────

def extract_article_text(text: str, article_id: str, next_pattern: str | None = None) -> dict[str, Any]:
    """Extract the full text of a specific article from a document.

    Args:
        text: Full document text.
        article_id: Article identifier, e.g. "第十四条".
        next_pattern: Regex to find the next article boundary.
                      If None, uses the first pattern from settings.

    Returns:
        {
            "found": true/false,
            "article_id": article identifier,
            "full_text": complete article text,
            "char_count": character count
        }
    """
    from config.settings import get_settings

    # Escape special chars in the article ID
    escaped_id = re.escape(article_id)
    pattern = re.compile(rf"{escaped_id}\s*[^\n]*")

    match = pattern.search(text)
    if not match:
        return {
            "found": False,
            "article_id": article_id,
            "full_text": "",
            "char_count": 0,
        }

    start = match.start()

    # Find the next article boundary using configurable pattern
    if next_pattern is None:
        settings = get_settings()
        next_pattern = settings.article_patterns[0] if settings.article_patterns else r"第[一-鿿\d]+条"
    next_article = re.compile(next_pattern).search(text, start + len(match.group()))
    end = next_article.start() if next_article else len(text)

    full_text = text[start:end].strip()
    return {
        "found": True,
        "article_id": article_id,
        "full_text": full_text,
        "char_count": len(full_text),
    }
