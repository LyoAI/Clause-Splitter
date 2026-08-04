"""Excel 解析节点 —— ReAct 模式.

使用 LLM 自动检测 Excel 列映射，提取条款数据。
支持多文档 Excel（多个制度名称 → 多个虚拟文档）。

Flow:
1. read_excel_preview() → 采样行发给 LLM
2. LLM schema 检测 (ReAct loop, max 3 次迭代)
3. extract_excel_articles() → 提取全部条款
4. 按 document_name 分组
5. 构建 DocumentStructure + 合成 raw_text → 汇入 plan_batches
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent.nodes import get_llm_from_state
from agent.tools.excel_parse_tools import (
    read_excel_preview,
    extract_excel_articles,
    _infer_article_id_from_text,
)
from agent.tools.utility_tools import estimate_complexity
from prompts.split.excel_parse import (
    SYSTEM_PROMPT,
    format_user_prompt,
    format_retry_feedback,
    format_samples_for_llm,
)
from config.settings import get_settings
from models.clause import DocumentStructure, ArticleInfo, ComplexityLabel
from utils import get_logger

logger = get_logger()


async def parse_excel_node(state: dict) -> dict:
    """ReAct 解析 Excel 文件为结构化条款。

    这是 Excel 输入路径的入口节点。它替代了 analyze_structure 的角色：
    对于 Excel 文件，条款边界已经由行给出，不需要 LLM 去"找"条款，
    只需要 LLM 去"理解"列映射。

    Args:
        state: AgentState，至少包含 file_path, document_name, llm。

    Returns:
        包含 document_structure, raw_text, total_articles 等的部分状态。
        这些字段与 analyze_structure_node 的输出兼容，
        因此可以直接馈入 plan_batches。
    """
    file_path = state.get("file_path", "")
    document_name = state.get("document_name", "")
    settings = get_settings()
    milestones = settings.progress_milestones

    if not file_path:
        return {
            "status": "failed",
            "error_message": "file_path 为空，无法解析 Excel",
            "current_step": "Excel 解析失败 —— file_path 为空",
        }

    logger.info("Excel 解析开始: %s (%s)", document_name, file_path)

    # ═══════════════════════════════════════════════════════════════════
    # Step 1: Read Excel preview
    # ═══════════════════════════════════════════════════════════════════
    preview = read_excel_preview(
        file_path,
        max_rows=settings.excel_max_sample_rows,
    )

    if preview.get("error"):
        return {
            "status": "failed",
            "error_message": f"Excel 预览读取失败: {preview['error']}",
            "current_step": "Excel 解析失败 —— 无法读取文件",
        }

    samples = preview.get("samples", [])
    total_rows = preview.get("total_rows", 0)

    if not samples:
        return {
            "status": "failed",
            "error_message": "Excel 文件中没有数据行（仅表头或完全为空）",
            "current_step": "Excel 解析失败 —— 无数据行",
        }

    if total_rows > settings.max_excel_rows_per_document:
        logger.warning(
            "Excel 行数过多 (%d)，超过上限 (%d)，将被截断",
            total_rows, settings.max_excel_rows_per_document,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Step 2: LLM schema detection (ReAct loop)
    # ═══════════════════════════════════════════════════════════════════
    llm = get_llm_from_state(state)
    column_mapping: dict[str, str] | None = None
    format_info: dict[str, Any] | None = None

    if llm is not None:
        sample_text = format_samples_for_llm(preview)
        sheet_name = preview.get("active_sheet", "")
        file_name = Path(file_path).name

        user_prompt = format_user_prompt(
            sample_rows_text=sample_text,
            sheet_name=sheet_name,
            total_rows=total_rows,
            file_name=file_name,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        for iteration in range(settings.excel_max_react_iterations):
            try:
                response = llm.invoke(messages)
                content = response.content if hasattr(response, "content") else str(response)

                parsed = _parse_llm_excel_output(content)

                if parsed and "column_mapping" in parsed:
                    cm = parsed["column_mapping"]
                    if cm.get("article_text"):
                        column_mapping = cm
                        format_info = parsed
                        logger.info(
                            "Excel schema 检测成功 (iter %d/%d): format=%s, confidence=%s, "
                            "mapping=%s, multi_doc=%s",
                            iteration + 1,
                            settings.excel_max_react_iterations,
                            parsed.get("format_type", "?"),
                            parsed.get("confidence", "?"),
                            {k: v for k, v in cm.items() if v},
                            parsed.get("is_multi_document", False),
                        )
                        break

                # Parse failed — append feedback and retry
                logger.warning(
                    "Excel schema 检测 iter %d: 输出无法解析或缺少 article_text", iteration + 1,
                )
                messages.append({"role": "assistant", "content": content[:1000]})
                messages.append({
                    "role": "user",
                    "content": format_retry_feedback(
                        "column_mapping 缺失或 article_text 字段为空"
                    ),
                })

            except Exception as e:
                logger.error("Excel schema 检测 iter %d 异常: %s", iteration + 1, e)
                if iteration < settings.excel_max_react_iterations - 1:
                    messages.append({
                        "role": "user",
                        "content": format_retry_feedback(str(e)),
                    })

    # ═══════════════════════════════════════════════════════════════════
    # Step 2b: Fallback — heuristic column detection without LLM
    # ═══════════════════════════════════════════════════════════════════
    if column_mapping is None:
        logger.warning("LLM schema 检测失败，使用启发式回退方案")
        column_mapping = _heuristic_column_detection(preview)
        format_info = {
            "format_type": "未知（启发式检测）",
            "confidence": "low",
            "reasoning": "LLM 检测失败，使用正则/关键词匹配表头",
        }

    if column_mapping is None or not column_mapping.get("article_text"):
        return {
            "status": "failed",
            "error_message": (
                "无法识别 Excel 的列结构。"
                "请确保 Excel 第一行是表头，且包含条款内容列（如'条款内容'、'法规内容'等）。"
                f"检测到的表头: {list(preview.get('column_headers', {}).values())}"
            ),
            "current_step": "Excel 解析失败 —— 无法识别列结构",
        }

    # ═══════════════════════════════════════════════════════════════════
    # Step 3: Extract all articles using the mapping
    # ═══════════════════════════════════════════════════════════════════
    extraction = extract_excel_articles(
        file_path=file_path,
        column_mapping=column_mapping,
        sheet_name=preview.get("active_sheet"),
        max_rows=settings.max_excel_rows_per_document,
    )

    if extraction.get("error"):
        return {
            "status": "failed",
            "error_message": f"Excel 条款提取失败: {extraction['error']}",
            "current_step": "Excel 解析失败 —— 提取阶段出错",
        }

    articles_data = extraction.get("articles", [])
    doc_groups = extraction.get("document_groups", {})
    unique_docs = extraction.get("unique_documents", [])

    if not articles_data:
        return {
            "status": "failed",
            "error_message": "从 Excel 中未提取到任何有效条款。请检查文件内容。",
            "current_step": "Excel 解析失败 —— 无有效条款",
        }

    # ═══════════════════════════════════════════════════════════════════
    # Step 4: Convert to ArticleInfo objects
    # ═══════════════════════════════════════════════════════════════════
    articles: list[ArticleInfo] = []
    for ad in articles_data:
        text = ad.get("full_text", "")
        art_id = ad.get("article_id", "")
        section = ad.get("section", "")
        doc_name = ad.get("document_name", "")

        # Infer article ID if missing
        if not art_id:
            art_id = _infer_article_id_from_text(text)

        complexity = estimate_complexity(text)

        # Build parent_section: combine document_name + section for context
        parent_section_parts = [p for p in [doc_name, section] if p]
        parent_section = " › ".join(parent_section_parts) if parent_section_parts else None

        articles.append(ArticleInfo(
            article_id=art_id,
            full_text=text,
            parent_section=parent_section,
            complexity_score=complexity["complexity_score"],
            complexity_label=(
                ComplexityLabel.COMPLEX
                if complexity["complexity_score"] >= settings.complex_solo_threshold
                else ComplexityLabel.SIMPLE
            ),
            estimated_obligation_count=max(1, complexity["metrics"]["sentence_count"]),
        ))

    # ═══════════════════════════════════════════════════════════════════
    # Step 5: Build DocumentStructure + synthetic raw_text
    # ═══════════════════════════════════════════════════════════════════
    doc_title = document_name or Path(file_path).stem

    doc_structure = DocumentStructure(
        document_title=doc_title,
        articles=articles,
        total_character_count=sum(len(a.full_text) for a in articles),
        structure_type="excel_structured",
    )

    # Build synthetic raw_text for downstream use (validation, context, etc.)
    raw_text_parts: list[str] = []
    for doc_key in unique_docs:
        group_articles = doc_groups.get(doc_key, [])
        if len(unique_docs) > 1:
            raw_text_parts.append(f"## {doc_key}")
        for art in group_articles:
            art_id = art.get("article_id", "")
            art_text = art.get("full_text", "")
            raw_text_parts.append(f"{art_id} {art_text}" if art_id else art_text)
        raw_text_parts.append("")

    synthetic_raw_text = "\n".join(raw_text_parts)

    # Build per-document grouping metadata
    excel_document_groups = [
        {
            "document_name": doc_key,
            "article_count": len(group_articles),
        }
        for doc_key, group_articles in doc_groups.items()
    ]

    # ═══════════════════════════════════════════════════════════════════
    # Return — feeds directly into plan_batches
    # ═══════════════════════════════════════════════════════════════════
    logger.info(
        "Excel 解析完成: %d 条款 across %d 文档, %d chars, format=%s",
        len(articles),
        len(doc_groups),
        len(synthetic_raw_text),
        format_info.get("format_type", "?") if format_info else "?",
    )

    step_desc = (
        f"Excel 解析完成 —— {len(articles)} 条款, "
        f"{len(doc_groups)} 个文档"
    )
    if len(doc_groups) > 1:
        doc_list = ", ".join(
            f"{d['document_name']}({d['article_count']})"
            for d in excel_document_groups
        )
        step_desc += f" [{doc_list}]"

    return {
        "raw_text": synthetic_raw_text,
        "document_structure": doc_structure.model_dump(),
        "total_articles": len(articles),
        "excel_document_groups": excel_document_groups,
        "progress": milestones["structure_complete"],
        "current_step": step_desc,
        "status": "analyzing_structure",
    }


# ═══════════════════════════════════════════════════════════════════════════
# LLM output parsing
# ═══════════════════════════════════════════════════════════════════════════


def _parse_llm_excel_output(content: str) -> dict | None:
    """Parse LLM JSON output for Excel schema detection.

    Uses the same robust extraction pattern as analyze_structure_node:
    1. Try code block (```json ... ```)
    2. Try raw JSON parse
    3. Try first {...} block via regex

    Args:
        content: Raw LLM response text.

    Returns:
        Parsed dict with column_mapping etc., or None if unparseable.
    """
    if not content or not content.strip():
        return None

    # Strategy 1: code block
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", content)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 2: raw JSON
    try:
        return json.loads(content.strip())
    except json.JSONDecodeError:
        pass

    # Strategy 3: first {...} block
    m = re.search(r"\{[\s\S]+\}", content)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    return None


# ═══════════════════════════════════════════════════════════════════════════
# Heuristic fallback (no LLM / LLM failed)
# ═══════════════════════════════════════════════════════════════════════════


def _heuristic_column_detection(preview: dict) -> dict[str, str] | None:
    """Fallback column detection using keyword matching on headers.

    Used when the LLM is unavailable or fails to detect the schema.
    Tries to match known patterns for 内规/外规 column names.

    Args:
        preview: Dict from read_excel_preview().

    Returns:
        column_mapping dict, or None.
    """
    headers = preview.get("column_headers", {})
    if not headers:
        return None

    # Known column name patterns (normalized)
    ARTICLE_TEXT_KEYWORDS = [
        "条款内容", "法规条款", "制度条款", "条文内容",
        "clause", "article_text", "content",
    ]
    ARTICLE_ID_KEYWORDS = [
        "条款名称", "条款编号", "条款标题", "条目名称",
        "clause_name", "article_id", "title",
    ]
    DOC_NAME_KEYWORDS = [
        "内部制度名称", "外规名称", "法规名称", "制度名称", "文档名称", "文件名称",
        "document_name", "regulation_name",
    ]
    SECTION_KEYWORDS = [
        "章节", "所属章节", "章", "节",
        "section", "chapter",
    ]
    CONTEXT_KEYWORDS = [
        "制度内容", "外规内容", "法规内容", "全文",
        "document_content", "full_text",
    ]

    def _match(col_name: str, keywords: list[str]) -> bool:
        """Check if a normalized header matches any keyword."""
        from agent.tools.excel_parse_tools import _normalize_column_header
        norm = _normalize_column_header(col_name).lower()
        for kw in keywords:
            if kw.lower() in norm:
                return True
        return False

    mapping: dict[str, str] = {}

    for col_letter, col_name in sorted(headers.items()):
        if not col_name:
            continue

        if not mapping.get("article_text") and _match(col_name, ARTICLE_TEXT_KEYWORDS):
            mapping["article_text"] = col_name
        elif not mapping.get("article_id") and _match(col_name, ARTICLE_ID_KEYWORDS):
            mapping["article_id"] = col_name
        elif not mapping.get("document_name") and _match(col_name, DOC_NAME_KEYWORDS):
            mapping["document_name"] = col_name
        elif not mapping.get("section") and _match(col_name, SECTION_KEYWORDS):
            mapping["section"] = col_name
        elif not mapping.get("additional_context") and _match(col_name, CONTEXT_KEYWORDS):
            mapping["additional_context"] = col_name

    if mapping.get("article_text"):
        logger.info(
            "启发式列检测: article_text='%s', mapping=%s",
            mapping["article_text"],
            {k: v for k, v in mapping.items() if k != "article_text"},
        )
        return mapping

    # Last resort: find the column with the longest average text content
    return _find_text_column_by_content(preview)


def _find_text_column_by_content(preview: dict) -> dict[str, str] | None:
    """Find the most likely article_text column by analyzing cell content length.

    The column with the longest average text content per cell is likely the
    article_text column.

    Args:
        preview: Dict from read_excel_preview().

    Returns:
        {"article_text": "col_name"} or None.
    """
    samples = preview.get("samples", [])
    headers = preview.get("column_headers", {})

    if not samples or not headers:
        return None

    col_lengths: dict[str, list[int]] = {}
    for s in samples:
        cols = s.get("columns", {})
        for col_letter, value in cols.items():
            if col_letter not in col_lengths:
                col_lengths[col_letter] = []
            col_lengths[col_letter].append(len(value))

    # Find column with highest average length
    best_col = None
    best_avg = 0
    for col_letter, lengths in col_lengths.items():
        avg = sum(lengths) / len(lengths) if lengths else 0
        if avg > best_avg and avg > 20:  # At least 20 chars avg to be clause text
            best_avg = avg
            best_col = col_letter

    if best_col and best_col in headers:
        col_name = headers[best_col]
        logger.info("内容分析回退: 最长平均文本列 '%s' (%s) → article_text (avg %d chars)",
                     best_col, col_name, int(best_avg))
        return {"article_text": col_name}

    return None
