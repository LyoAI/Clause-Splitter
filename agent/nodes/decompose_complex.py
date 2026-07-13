"""复杂条款拆分节点 —— ReAct 多步推理.

处理复杂度 >= COMPLEX_SOLO_THRESHOLD 的复杂条款。
LLM 使用工具（decompose_article_batch, validate_obligation_schema）进行循环推理。
"""

from __future__ import annotations

import json

# state is plain dict
from typing import Any
from agent.nodes import get_llm_from_state
from agent.prompts.loader import PromptLoader
from agent.prompts.decompose_complex import (
    format_user_prompt,
    format_validation_error_feedback,
    format_unparseable_output_feedback,
)
from agent.tools.decompose_tools import decompose_article_batch, _parse_llm_output_to_obligations
from config.settings import get_settings
from models.clause import ClauseObligation
from utils import get_logger

logger = get_logger()


async def decompose_complex_node(state: dict) -> dict:
    """ReAct 多步分解复杂条款.

    对当前 batch 的复杂条款使用 ReAct 循环：
    1. 分析句子结构（识别主体/动词/委托关系）
    2. 调用 decompose_article_batch 工具拆分
    3. 调用 validate_obligation_schema 验证
    4. 如有验证错误，反馈并重试（最多 MAX_REACT_ITERATIONS 次）

    Args:
        state: 包含 batches, current_batch_index 的 AgentState。

    Returns:
        包含更新后 obligations 的部分状态。
    """
    batches = state.get("batches", [])
    batch_index = state.get("current_batch_index", 0)
    document_name = state.get("document_name", "")

    if batch_index >= len(batches):
        return {"current_step": "所有批次已处理完成"}

    current_batch = batches[batch_index]
    logger.info(
        "复杂拆分 (ReAct): 批次 %d/%d, %d 个条款",
        batch_index + 1, len(batches), len(current_batch),
    )

    loader = PromptLoader()
    system_prompt = loader.get_decompose_complex_prompt(document_name=document_name)
    llm = get_llm_from_state(state)
    max_iterations = get_settings().max_react_iterations

    existing_obligations = list(state.get("obligations", []))
    new_obligations: list[dict[str, Any]] = []

    for article in current_batch:
        article_id = article.get("article_id", "未知")
        article_text = article.get("full_text", "")

        # ReAct 循环
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": format_user_prompt(article_id, article_text)},
        ]

        for iteration in range(max_iterations):
            try:
                if llm is not None:
                    # 使用 LLM（无 tools 模式，LLM 在文本中告诉我们结果）
                    response = llm.invoke(messages)
                    content = response.content if hasattr(response, "content") else str(response)

                    # Try to parse YAML/JSON output
                    parsed = _parse_llm_output_to_obligations(content, article_id, document_name)

                    if parsed:
                        # 验证
                        valid_obls = []
                        for obl_dict in parsed:
                            try:
                                ClauseObligation(**obl_dict)
                                valid_obls.append(obl_dict)
                            except Exception as e:
                                logger.warning(
                                    "条款 %s 迭代 %d 验证失败: %s",
                                    article_id, iteration + 1, e,
                                )
                                # 反馈给 LLM 重试
                                messages.append({
                                    "role": "assistant",
                                    "content": content[:500],
                                })
                                messages.append({
                                    "role": "user",
                                    "content": format_validation_error_feedback(str(e)),
                                })
                                break  # 进入下一次迭代

                        if valid_obls:
                            new_obligations.extend(valid_obls)
                            logger.info(
                                "条款 %s ReAct 完成（迭代 %d）: %d 条义务",
                                article_id, iteration + 1, len(valid_obls),
                            )
                            break  # 成功，处理下一个条款
                    else:
                        # 无法解析，重试
                        messages.append({"role": "assistant", "content": content[:500]})
                        messages.append({
                            "role": "user",
                            "content": format_unparseable_output_feedback(),
                        })
                else:
                    # No LLM — use direct tool call
                    articles_json = json.dumps([{"id": article_id, "text": article_text}])
                    result = decompose_article_batch(
                        articles_json=articles_json,
                        document_name=document_name,
                    )
                    new_obligations.extend(result.get("obligations", []))
                    break

            except Exception as e:
                logger.error("条款 %s ReAct 迭代 %d 出错: %s", article_id, iteration + 1, e)
                if iteration == max_iterations - 1:
                    logger.error("条款 %s ReAct 达到最大迭代次数，跳过", article_id)
                break

    # 合并
    all_obligations = existing_obligations + new_obligations
    for i, obl in enumerate(all_obligations):
        obl["index"] = i + 1

    total_articles = state.get("total_articles", 0)
    prev_processed = state.get("processed_articles", 0)
    processed = min(prev_processed + len(current_batch), total_articles) if total_articles else prev_processed + len(current_batch)
    milestones = get_settings().progress_milestones
    progress = milestones["decompose_start"] + (processed / max(total_articles, 1)) * (milestones["decompose_end"] - milestones["decompose_start"])

    return {
        "obligations": all_obligations,
        "current_batch_index": batch_index + 1,
        "processed_articles": processed,
        "progress": progress,
        "current_step": f"ReAct拆分中…… {processed}/{total_articles} articles (batch {batch_index + 1}/{len(batches)})",
        "status": "decomposing",
    }
