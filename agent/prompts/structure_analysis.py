"""Document structure analysis prompt — for analyze_structure node (ReAct)."""

SYSTEM_PROMPT = """# 文档结构分析器

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


def format_user_prompt(text_snippet: str, total_chars: int, max_input: int) -> str:
    """Format the user prompt for structure analysis.

    Args:
        text_snippet: Truncated text to analyze.
        total_chars: Total character count of the full document.
        max_input: Max characters sent (so truncation note makes sense).

    Returns:
        Formatted user prompt string.
    """
    prompt = f"请分析以下文档的结构：\n\n{text_snippet}"
    if total_chars > max_input:
        prompt += (
            f"\n\n（注意：原文共 {total_chars} 字符，此处仅展示前 {max_input} 字符。"
            f"请基于已有内容识别所有可见条款，并用'...'标记截断位置。）"
        )
    return prompt
