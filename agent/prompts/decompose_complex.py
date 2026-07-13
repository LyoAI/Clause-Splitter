"""Complex clause decomposition prompt — for decompose_complex node (ReAct multi-step).

For complex clauses, the LLM uses tool-calling loops for multi-step reasoning.
"""

SYSTEM_PROMPT_TEMPLATE = """# 条款义务拆分器（复杂模式 / ReAct）

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


def format_user_prompt(article_id: str, article_text: str) -> str:
    """Format the initial user prompt for complex article decomposition.

    Args:
        article_id: Article identifier (e.g. '第十四条').
        article_text: Full text of the article.

    Returns:
        Formatted user prompt string.
    """
    return f"请拆分以下复杂条款：\n\n条款编号：{article_id}\n条款文本：\n{article_text}"


def format_validation_error_feedback(error_message: str) -> str:
    """Format a validation error feedback message for ReAct retry.

    Args:
        error_message: The validation error to report back to the LLM.

    Returns:
        Formatted feedback string.
    """
    return f"验证失败：{error_message}\n请修正后重新输出。"


def format_unparseable_output_feedback() -> str:
    """Format feedback when LLM output cannot be parsed."""
    return "无法解析输出。请严格按照 YAML 格式重新输出。"
