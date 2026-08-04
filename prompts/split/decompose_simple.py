"""Simple clause decomposition prompt — for decompose_simple node (One-shot LLM).

For simple clauses, use one-shot structured output to avoid unnecessary ReAct overhead.
"""

SYSTEM_PROMPT_TEMPLATE = """# 条款义务拆分器（简单模式）

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


def format_user_prompt(article_id: str, article_text: str) -> str:
    """Format the user prompt for a single article (used by complex path)."""
    return f"""请拆分以下条款：

条款编号：{article_id}
条款文本：
{article_text}
"""


def format_batch_user_prompt(articles: list[dict]) -> str:
    """Pack multiple simple articles into one prompt for batched LLM call.

    Each obligation in the LLM output must include its source 条款编号.
    """
    lines = [
        f"请逐一拆分以下 {len(articles)} 个条款。每个条款的每条义务记录中必须正确标注对应的条款编号。",
        "",
    ]
    for art in articles:
        aid = art.get("article_id", "?")
        text = art.get("full_text", "")
        lines.append(f"---")
        lines.append(f"条款编号：{aid}")
        lines.append(f"条款文本：{text}")
        lines.append("")
    return "\n".join(lines)
