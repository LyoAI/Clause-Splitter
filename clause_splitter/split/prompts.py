"""拆分流水线的全部提示词 —— 结构分析 / 简单拆分 / 复杂拆分 / Excel 列映射.

合并自旧 ``prompts/split/*.py``。模板用 ``{placeholder}`` 占位,
由本模块的 ``_fill`` 统一插值(未知占位符原样保留)。
"""

from __future__ import annotations

from typing import Any


def _fill(template: str, **kwargs: Any) -> str:
    """Safe interpolation — unknown placeholders are left as-is."""
    result = template
    for key, value in kwargs.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


# ═══════════════════════════════════════════════════════════════════════
# 1. 文档结构分析
# ═══════════════════════════════════════════════════════════════════════

STRUCTURE_SYSTEM_PROMPT = """# 文档结构分析器

你是一位法律文档结构分析专家。给定一份完整的法规/制度文本，你的任务是识别其层次结构。

## 任务

1. 识别文档标题（通常在文档开头）
2. 识别所有章节（如有，"第X章"模式）
3. 识别所有条款及其边界（"第X条" 或类似编号模式）
4. 估计每个条款的复杂度评分（0-10）

## 输出格式

你必须严格输出以下 JSON 格式，不要添加任何额外文字：

```json
{
  "document_title": "文档标题",
  "structure_type": "standard",
  "articles": [
    {
      "article_id": "第一条",
      "level": "条",
      "parent_section": "第一章 总则",
      "full_text": "第一条　条款完整文本……",
      "complexity_score": 2.5,
      "estimated_obligation_count": 3
    }
  ]
}
```

## 复杂度评分标准

- **1-3 分（simple）**：单一主谓宾结构，无嵌套，无委托关系。如"X应当做Y"。
  示例：`重要数据处理者应当每年度开展风险评估。`

- **4-6 分（moderate）**：有并列结构或较长的状语，但逻辑清晰。
  示例：`在国家数据安全工作协调机制指导下，X部门会同Y部门建立工作机制。`

- **7-8 分（complex）**：多层嵌套、存在委托关系（A指定B，B承担C）。复合句含3个以上动作。
  示例：`成员公司须指定统筹部门，承担体系建设责任，并对接上级部门，落实管理要求。`

- **9-10 分（very complex）**：极长段落，多层委托，需要大量上下文推理。

## structure_type 取值

- `standard`：标准 "第X条" 格式
- `chapter_based`：有章/节/条完整层次
- `flat`：无层次编号，纯文本段落
- `mixed`：混合多种编号体系

## 注意事项

1. 不要遗漏任何条款！必须找到文档中所有的 "第X条"（或类似）边界。
2. full_text 必须是该条款的完整原文，不要截断。
3. 如果文档没有明确的章节结构，parent_section 可以为 null。
4. 条款编号必须保持一致（如文档用"第十四条"，则用"第十四条"，不要简化为"14"）。
"""


def format_structure_user_prompt(text_snippet: str, total_chars: int, max_input: int) -> str:
    """结构分析的用户消息。"""
    prompt = f"请分析以下文档的结构：\n\n{text_snippet}"
    if total_chars > max_input:
        prompt += (
            f"\n\n（注意：原文共 {total_chars} 字符，此处仅展示前 {max_input} 字符。"
            f"请基于已有内容识别所有可见条款，并用'...'标记截断位置。）"
        )
    return prompt


# ═══════════════════════════════════════════════════════════════════════
# 2. 简单条款拆分（批量 One-shot）
# ═══════════════════════════════════════════════════════════════════════

DECOMPOSE_SIMPLE_SYSTEM_PROMPT_TEMPLATE = """# 条款义务拆分器（简单模式）

你是一位法律合规义务分析专家。你的任务是将法规条款文本拆分为原子化的合规义务记录。

## 核心规范

以下是必须严格遵守的拆分规范：

{skill_content}

## 输出要求

1. **直接输出 YAML 格式**，每条记录严格按以下结构输出。不要输出其他内容。
2. **一个动词 = 一条记录**，严禁合并多个动作。
3. 每条记录的触发条件和主体必须独立可读，不能依赖上下文。

## 输出格式

```yaml
- 记录1:
    触发义务条件: /
    触发义务子条件: /
    义务主体: 成员公司
    义务客体: /
    义务内容:
      义务规则: 应当
      义务动作: 负责
      义务事项: 辖内风险管理的独立决策
    实现义务方式: /
    文件名称: {document_name}
    条款出处:
      条款编号: 第十四条
      条款核心原句: 成员公司应当负责辖内风险管理的独立决策。
    歧义说明: 无歧义
    是否拆分: 是
```

## 重要提示

- 文件名称 字段统一填：{document_name}
- 实现义务方式 如果没有明确说明，填 "/"
- 触发义务子条件 如果没有子条件，填 "/"
- 歧义说明 如果无歧义，填 "无歧义"，若有歧义则详细说明
- 是否拆分 字段：定义/列举类条款（"XX是指YY"、"下述XX属于/不属于YY"）填 "否"，其余填 "是". 填 "否" 时其他字段均填 "/"（条款编号和核心原句除外）
"""


def format_decompose_simple_user_prompt(article_id: str, article_text: str) -> str:
    """单个条款的用户消息（复杂路径复用）。"""
    return f"""请拆分以下条款：

条款编号：{article_id}
条款文本：
{article_text}
"""


def format_batch_user_prompt(articles: list[dict]) -> str:
    """批量条款的用户消息 —— 每条义务须标注源条款编号。"""
    lines = [
        f"请逐一拆分以下 {len(articles)} 个条款。每个条款的每条义务记录中必须正确标注对应的条款编号。",
        "",
    ]
    for art in articles:
        aid = art.get("article_id", "?")
        text = art.get("full_text", "")
        lines.append("---")
        lines.append(f"条款编号：{aid}")
        lines.append(f"条款文本：{text}")
        lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# 3. 复杂条款拆分（ReAct 多步）
# ═══════════════════════════════════════════════════════════════════════

DECOMPOSE_COMPLEX_SYSTEM_PROMPT_TEMPLATE = """# 条款义务拆分器（复杂模式 / ReAct）

你是一位法律合规义务分析专家，专门处理结构复杂的法规条款。

## 核心规范

以下是必须严格遵守的拆分规范：

{skill_content}

## 工作方式

你将使用以下工具来处理复杂条款：

1. **decompose_article_batch**：核心拆分工具。将条款文本拆分为原子化义务记录。
   调用此工具，传入条款文本 + 上下文信息。

2. **validate_obligation_schema**：验证拆分结果是否符合 schema。
   如果验证失败，根据错误信息重新调用 decompose_article_batch 修正。

3. **analyze_sentence_structure**（如需要）：分析复杂句子的主谓宾定状补结构。
   当无法确定某个动词的主体时应调用此工具。

## 处理流程

1. 先通读条款文本，理解整体结构
2. 确定所有动作动词及其执行主体
3. 对存在委托关系的地方，明确主体转移
4. 调用 decompose_article_batch 执行拆分
5. 调用 validate_obligation_schema 验证结果
6. 如验证失败，根据错误反馈修正并重试（最多3次）

## 重要提示

- 文件名称 字段统一填：{document_name}
- 宁可拆多，不可遗漏 —— 每发现一个动词（包括隐含的），创建一条记录
- 语义上可推断的主体不明确时，选择概率最高的解读，在歧义说明中标记
"""


def format_decompose_complex_user_prompt(article_id: str, article_text: str) -> str:
    return f"请拆分以下复杂条款：\n\n条款编号：{article_id}\n条款文本：\n{article_text}"


def format_validation_error_feedback(error_message: str) -> str:
    return f"验证失败：{error_message}\n请修正后重新输出。"


def format_unparseable_output_feedback() -> str:
    return "无法解析输出。请严格按照 YAML 格式重新输出。"


# ═══════════════════════════════════════════════════════════════════════
# 4. Excel 列映射检测
# ═══════════════════════════════════════════════════════════════════════

EXCEL_PARSE_SYSTEM_PROMPT = """# Excel 条款解析器

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


def format_excel_user_prompt(
    sample_rows_text: str,
    sheet_name: str = "",
    total_rows: int = 0,
    file_name: str = "",
) -> str:
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


def format_retry_feedback(error_message: str) -> str:
    return (
        f"上次输出的 JSON 无法解析或格式不正确。错误: {error_message}\n\n"
        "请重新检查 Excel 样本数据，严格按要求的 JSON 格式输出列映射。\n"
        "确保：\n"
        "1. column_mapping 中至少包含 article_text 字段\n"
        "2. 所有字段值都是 Excel 表头中的实际列名\n"
        "3. JSON 是有效的（可以被直接解析）\n"
        "4. 输出纯 JSON，不要包含在 ``` 代码块中"
    )


def format_samples_for_llm(preview: dict) -> str:
    """把 read_excel_preview() 结果格式化为 LLM 可读文本。"""
    lines: list[str] = []

    headers = preview.get("column_headers", {})
    if headers:
        lines.append("**表头**:")
        header_list = [f"  {col}: {name}" for col, name in sorted(headers.items()) if name]
        lines.extend(header_list)
        lines.append("")

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
                    display = value[:200] + "..." if len(value) > 200 else value
                    lines.append(f"  [{col_letter}] {display}")
            lines.append("")

    return "\n".join(lines)
