"""简单条款拆分节点 —— One-shot LLM + structured output.

处理复杂度 < COMPLEX_SOLO_THRESHOLD 的简单条款批次。
使用 LangChain with_structured_output 一次性提取。
"""

from __future__ import annotations

import json
from typing import Any

# state is plain dict
from agent.nodes import get_llm_from_state
from agent.prompts.loader import PromptLoader
from agent.prompts.decompose_simple import format_user_prompt
from agent.tools.decompose_tools import decompose_article_batch, _parse_llm_output_to_obligations
from config.settings import get_settings
from models.clause import ClauseObligation
from utils import get_logger

logger = get_logger()


async def decompose_simple_node(state: dict) -> dict:
    """One-shot 拆分简单条款.

    对当前 batch 的每个条款调用 LLM，直接输出结构化 YAML/JSON，
    然后通过 Pydantic 验证。

    Args:
        state: 包含 batches, current_batch_index, obligations 的 AgentState。

    Returns:
        包含更新后的 obligations 及进度。
    """
    batches = state.get("batches", [])
    batch_index = state.get("current_batch_index", 0)
    document_name = state.get("document_name", "")

    if batch_index >= len(batches):
        return {"current_step": "所有批次已处理完成"}

    current_batch = batches[batch_index]
    logger.info(
        "简单拆分: 批次 %d/%d, %d 个条款",
        batch_index + 1, len(batches), len(current_batch),
    )

    # 加载 prompt
    loader = PromptLoader()
    system_prompt = loader.get_decompose_simple_prompt(document_name=document_name)

    # Get LLM instance from state
    llm = get_llm_from_state(state)

    new_obligations: list[dict[str, Any]] = []
    existing_obligations = state.get("obligations", [])

    for article in current_batch:
        article_id = article.get("article_id", "未知")
        article_text = article.get("full_text", "")

        user_prompt = format_user_prompt(article_id, article_text)

        try:
            if llm is not None:
                # 使用 LLM
                response = llm.invoke([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ])
                content = response.content if hasattr(response, "content") else str(response)
            else:
                # No LLM — use direct function call
                articles_json = json.dumps([{"id": article_id, "text": article_text}])
                result = decompose_article_batch(
                    articles_json=articles_json,
                    document_name=document_name,
                )
                content = json.dumps(result.get("obligations", []))

            # Parse LLM output
            parsed = _parse_llm_output_to_obligations(content, article_id, document_name)

            for obl_dict in parsed:
                try:
                    ClauseObligation(**obl_dict)
                    new_obligations.append(obl_dict)
                except Exception as e:
                    logger.warning("条款 %s 验证失败: %s", article_id, e)

        except Exception as e:
            logger.error("拆分条款 %s 失败: %s", article_id, e)

    # 合并到现有 obligations
    all_obligations = list(existing_obligations) + new_obligations

    # Re-number
    for i, obl in enumerate(all_obligations):
        obl["index"] = i + 1

    total_articles = state.get("total_articles", 0)
    milestones = get_settings().progress_milestones
    processed = min(batch_index + len(current_batch), total_articles) if total_articles else batch_index + len(current_batch)
    progress = milestones["decompose_start"] + (processed / max(total_articles, 1)) * (milestones["decompose_end"] - milestones["decompose_start"])

    logger.info(
        "批次 %d 完成: 新增 %d 条义务，累计 %d 条",
        batch_index + 1, len(new_obligations), len(all_obligations),
    )

    return {
        "obligations": all_obligations,
        "current_batch_index": batch_index + 1,
        "processed_articles": processed,
        "progress": progress,
        "current_step": f"条款拆分中…… {processed}/{total_articles}",
        "status": "decomposing",
    }
