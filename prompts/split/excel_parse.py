"""Excel schema detection prompts — LLM-powered column mapping for clause data.

When an Excel file is uploaded, the LLM receives a preview of the headers
and sample rows. It must:
1. Identify which columns contain clause text (required for splitting)
2. Map optional metadata columns (document name, article ID, section, etc.)
3. Detect whether this is 内规 (internal) or 外规 (external) format
4. Detect whether the Excel contains multiple documents

This approach avoids hard-coding column names — the LLM handles variation.
"""

from __future__ import annotations

# ── System prompt template ─────────────────────────────────────────────────

SYSTEM_PROMPT = """# Excel 条款解析器

你是一个数据映射专家。给定一个 Excel 文件的前几行预览数据，你需要识别列结构并输出标准化的列映射。

## 任务

1. **识别条款内容列** — 这是必须的。条款内容列包含需要拆分的法律条款全文（如"第X条 ……"）
2. **识别元数据列** — 可选的辅助列：
   - 文档/制度名称（如"内部制度名称"、"外规名称"）
   - 条款编号（如"条款名称"、"条款编号"）
   - 所属章节（如"章节"、"所属章节"）
   - 补充上下文（如"制度内容"、"外规内容"——包含该条款所在文件的概要信息）
3. **判断格式类型** — "内规" / "外规" / "其他"
4. **判断是否多文档** — 文件中是否包含多个不同的制度/法规名称

## 判断依据

- **内规格式特征**：通常有"内部制度名称"、"IOBS_KEY"等列，指向企业内部制度
- **外规格式特征**：通常有"外规名称"、"URL"等列，指向外部法律法规
- **条款内容列特征**：内容较长，包含"第X条"、"应当"、"禁止"等法律条文特征
- **多文档判断**：如果"文档名称"列有多个不同值，则为多文档

## 输出格式

严格输出以下 JSON（不要包含在代码块中，直接输出纯 JSON）：

{
  "format_type": "内规",
  "is_multi_document": false,
  "confidence": "high",
  "column_mapping": {
    "article_text": "条款内容",
    "article_id": "条款名称",
    "document_name": "内部制度名称",
    "section": "章节",
    "additional_context": "制度内容"
  },
  "reasoning": "检测到6个列。'条款内容'列包含完整的法律条文（如'第十四条 数据处理者应当……'），确定为 article_text。'内部制度名称'列为文档名称。未检测到多个不同的制度名称，判定为单文档。"
}

## 字段说明

- `format_type`: "内规" | "外规" | "其他"
- `is_multi_document`: true | false
- `confidence`: "high"（列含义清晰）| "medium"（有推断）| "low"（不确定）
- `column_mapping`:
  - `article_text`（必填）：条款全文所在的列名
  - `article_id`（可选）：条款编号所在的列名
  - `document_name`（可选）：文档/制度名称所在的列名
  - `section`（可选）：章节所在的列名
  - `additional_context`（可选）：补充上下文所在的列名
- `reasoning`: 1-3句话简要说明判断过程

## 注意事项

1. `article_text` 是唯一必填字段，无法识别时应标记 confidence 为 "low" 并说明原因
2. 列名可能有变化（如"条款内容"可能写作"法规条款内容"、"条款正文"等），请根据**列内容**（而非列名）判断
3. 如果 Excel 表格没有条款文本，全部是数字/日期/ID 等，标记 format_type 为 "无法识别"
4. 注意区分"制度内容"（全文概要）和"条款内容"（具体条款）——前者通常是一个大段文字，后者是逐条的条款
"""


# ── User prompt formatter ──────────────────────────────────────────────────


def format_user_prompt(
    sample_rows_text: str,
    sheet_name: str = "",
    total_rows: int = 0,
    file_name: str = "",
) -> str:
    """Format the user prompt with Excel preview data.

    Args:
        sample_rows_text: Pre-formatted sample rows text from read_excel_preview.
        sheet_name: Active sheet name.
        total_rows: Approximate total data rows.
        file_name: Original file name for context.

    Returns:
        Formatted user prompt string.
    """
    parts = [
        "请分析以下 Excel 文件的列结构：",
        "",
        f"**文件名**: {file_name}" if file_name else "",
        f"**工作表**: {sheet_name}" if sheet_name else "",
        f"**预估数据行数**: {total_rows}" if total_rows else "",
        "",
        "## 表头与样本数据",
        "",
        sample_rows_text,
        "",
        "请输出列映射 JSON。",
    ]
    return "\n".join(line for line in parts if line)


# ── Retry feedback formatter ───────────────────────────────────────────────


def format_retry_feedback(error_message: str) -> str:
    """Format feedback for ReAct retry when LLM output is unparseable.

    Args:
        error_message: Description of what went wrong with the previous output.

    Returns:
        Feedback string to append to the conversation.
    """
    return (
        f"上次输出的 JSON 无法解析或格式不正确。错误: {error_message}\n\n"
        "请重新检查 Excel 样本数据，严格按要求的 JSON 格式输出列映射。\n"
        "确保：\n"
        "1. column_mapping 中至少包含 article_text 字段\n"
        "2. 所有字段值都是 Excel 表头中的实际列名\n"
        "3. JSON 是有效的（可以被直接解析）\n"
        "4. 输出纯 JSON，不要包含在 ``` 代码块中"
    )


# ── Sample data formatter ──────────────────────────────────────────────────


def format_samples_for_llm(preview: dict) -> str:
    """Format the read_excel_preview() result as human-readable text for the LLM.

    Args:
        preview: Dict returned by read_excel_preview().

    Returns:
        Formatted sample data text.
    """
    lines: list[str] = []

    # Column headers
    headers = preview.get("column_headers", {})
    if headers:
        lines.append("**表头**:")
        header_list = [f"  {col}: {name}" for col, name in sorted(headers.items()) if name]
        lines.extend(header_list)
        lines.append("")

    # Sample rows
    samples = preview.get("samples", [])
    if samples:
        lines.append(f"**样本数据 (前 {len(samples)} 行)**:")
        lines.append("")
        for s in samples:
            row_num = s.get("row", "?")
            lines.append(f"--- 第 {row_num} 行 ---")
            cols = s.get("columns", {})
            for col_letter, value in sorted(cols.items()):
                if value:
                    # Truncate very long values
                    display = value[:200] + "..." if len(value) > 200 else value
                    lines.append(f"  [{col_letter}] {display}")
            lines.append("")

    return "\n".join(lines)
