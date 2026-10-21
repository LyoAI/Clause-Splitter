"""条款义务拆分 —— 简单(批量 One-shot) / 复杂(逐条 ReAct).

批次按复杂度分派:
- 简单批次:同批条款打包成一次 LLM 调用,输出合并 YAML。
- 复杂批次:逐条款多步推理,验证失败反馈重试(≤ max_react_iterations)。

LLM 调用统一走 ``ainvoke``(新 facade),无 LLM 时返回空列表(拆分必须依赖 LLM)。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from clause_splitter.config import get_settings
from clause_splitter.models import ClauseObligation, ObligationRule
from clause_splitter.prompts import load, render
from clause_splitter.utils import get_logger
from clause_splitter.utils.json_utils import parse_json_from_text

logger = get_logger()


# ═══════════════════════════════════════════════════════════════════════
# 批次分派
# ═══════════════════════════════════════════════════════════════════════


async def decompose_batch(
    batch: list[dict[str, Any]],
    document_name: str = "",
    llm: Any = None,
) -> list[dict[str, Any]]:
    """拆分一个批次,返回义务记录 dict 列表。

    Args:
        batch: 条款 dict 列表(含 article_id / full_text / complexity_score)。
        document_name: 文档名称,填入"文件名称"字段。
        llm: LLM 实例(需要 ``ainvoke``);None 时返回空列表并告警。

    Returns:
        list[ClauseObligation dict]。
    """
    if llm is None:
        logger.warning("decompose_batch: llm 为 None,跳过批次 %d 条条款", len(batch))
        return []

    settings = get_settings()
    has_complex = any(
        a.get("complexity_score", 1.0) >= settings.complex_solo_threshold for a in batch
    )

    if has_complex:
        logger.info("复杂拆分: 批次内 %d 条条款 (逐条)", len(batch))
        return await _decompose_complex(batch, document_name, llm)
    logger.info("简单拆分: 批次内 %d 条条款 (批量)", len(batch))
    return await _decompose_simple(batch, document_name, llm)


# ═══════════════════════════════════════════════════════════════════════
# 简单批量
# ═══════════════════════════════════════════════════════════════════════


def _batch_user_prompt(articles: list[dict[str, Any]]) -> str:
    """批量条款的用户消息 —— 每条义务须标注源条款编号。"""
    lines = [
        f"请逐一拆分以下 {len(articles)} 个条款。每个条款的每条义务记录中必须正确标注对应的条款编号。",
        "",
    ]
    for art in articles:
        lines += [
            "---",
            f"条款编号：{art.get('article_id', '?')}",
            f"条款文本：{art.get('full_text', '')}",
            "",
        ]
    return "\n".join(lines)


async def _decompose_simple(
    batch: list[dict[str, Any]],
    document_name: str,
    llm: Any,
) -> list[dict[str, Any]]:
    system_prompt = render(
        "obligation_decompose_simple",
        skill_content=load("obligation_spec"),
        document_name=document_name,
    )
    user_prompt = _batch_user_prompt(batch)

    try:
        content = await _llm_call(llm, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
    except Exception as e:
        logger.error("简单批次 LLM 调用失败: %s", e)
        return []

    # 条款编号由 YAML 自身标注,空 article_id 仅作回退
    parsed = _parse_llm_output_to_obligations(content, "", document_name)
    return _validate_records(parsed)


# ═══════════════════════════════════════════════════════════════════════
# 复杂逐条(ReAct 多步 + 反馈重试)
# ═══════════════════════════════════════════════════════════════════════


async def _decompose_complex(
    batch: list[dict[str, Any]],
    document_name: str,
    llm: Any,
) -> list[dict[str, Any]]:
    settings = get_settings()
    system_prompt = render(
        "obligation_decompose_complex",
        skill_content=load("obligation_spec"),
        document_name=document_name,
    )
    max_iterations = settings.max_react_iterations

    new_obligations: list[dict[str, Any]] = []

    for article in batch:
        article_id = article.get("article_id", "未知")
        article_text = article.get("full_text", "")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                f"请拆分以下复杂条款：\n\n条款编号：{article_id}\n条款文本：\n{article_text}"
            )},
        ]

        for iteration in range(max_iterations):
            try:
                content = await _llm_call(llm, messages)
                parsed = _parse_llm_output_to_obligations(content, article_id, document_name)

                if parsed:
                    valid = _validate_records(parsed, silent=True)
                    if valid:
                        new_obligations.extend(valid)
                        logger.info(
                            "条款 %s ReAct 完成（迭代 %d）: %d 条义务",
                            article_id, iteration + 1, len(valid),
                        )
                        break
                    # 全部验证失败 → 反馈给 LLM 重试
                    messages.append({"role": "assistant", "content": content[:500]})
                    messages.append({
                        "role": "user",
                        "content": "验证失败：记录不符合 schema\n请修正后重新输出。",
                    })
                else:
                    messages.append({"role": "assistant", "content": content[:500]})
                    messages.append({
                        "role": "user",
                        "content": "无法解析输出。请严格按照 YAML 格式重新输出。",
                    })
            except Exception as e:
                logger.error("条款 %s ReAct 迭代 %d 出错: %s", article_id, iteration + 1, e)
                break

    return new_obligations


# ═══════════════════════════════════════════════════════════════════════
# LLM 调用封装
# ═══════════════════════════════════════════════════════════════════════


async def _llm_call(llm: Any, messages: list[dict[str, Any]]) -> str:
    """调用 LLM 并返回文本内容(兼容 ainvoke / invoke)。"""
    if hasattr(llm, "ainvoke"):
        response = await llm.ainvoke(messages)
    else:
        response = await asyncio.to_thread(llm.invoke, messages)
    return response.content if hasattr(response, "content") else str(response)


def _validate_records(
    parsed: list[dict[str, Any]],
    silent: bool = False,
) -> list[dict[str, Any]]:
    """Pydantic 验证并返回合法记录(丢弃非法项)。"""
    valid: list[dict[str, Any]] = []
    for obl_dict in parsed:
        try:
            ClauseObligation(**obl_dict)
            valid.append(obl_dict)
        except Exception as e:
            if not silent:
                logger.warning("义务验证失败: %s", e)
    return valid


# ═══════════════════════════════════════════════════════════════════════
# Schema 验证工具
# ═══════════════════════════════════════════════════════════════════════


def validate_obligation_schema(obligations_json: str) -> dict[str, Any]:
    """验证一组义务记录是否符合 Pydantic schema。"""
    try:
        data = json.loads(obligations_json)
    except json.JSONDecodeError as e:
        return {
            "all_valid": False, "total": 0, "valid_count": 0,
            "errors": [{"index": -1, "message": f"JSON 解析失败: {e}", "record_preview": ""}],
        }

    if not isinstance(data, list):
        data = [data]

    errors: list[dict[str, Any]] = []
    valid_count = 0

    for i, record in enumerate(data):
        try:
            ClauseObligation(**record)
            valid_count += 1
        except Exception as e:
            errors.append({"index": i, "message": str(e), "record_preview": str(record)[:200]})

    return {"all_valid": len(errors) == 0, "total": len(data), "valid_count": valid_count, "errors": errors}


# ═══════════════════════════════════════════════════════════════════════
# LLM 输出解析 / 字段标准化
# ═══════════════════════════════════════════════════════════════════════


def _parse_llm_output_to_obligations(
    content: str, article_id: str, document_name: str,
) -> list[dict[str, Any]]:
    """将 LLM 输出的 YAML/JSON/文本 解析为 ClauseObligation dict 列表。"""
    import yaml

    obligations: list[dict[str, Any]] = []

    # 尝试1: 直接 YAML 解析
    try:
        parsed = yaml.safe_load(content)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    obl = _normalize_obligation(item, article_id, document_name)
                    if obl:
                        obligations.append(obl)
            if obligations:
                return obligations
    except Exception:
        pass

    # 尝试2: JSON 解析
    try:
        json_data = parse_json_from_text(content)
        if isinstance(json_data, list):
            for item in json_data:
                obl = _normalize_obligation(item, article_id, document_name)
                if obl:
                    obligations.append(obl)
        elif isinstance(json_data, dict):
            obl = _normalize_obligation(json_data, article_id, document_name)
            if obl:
                obligations.append(obl)
        if obligations:
            return obligations
    except Exception:
        pass

    # 尝试3: 提取 YAML 块
    yaml_match = re.search(r"```(?:yaml)?\s*([\s\S]+?)\s*```", content)
    if yaml_match:
        try:
            parsed = yaml.safe_load(yaml_match.group(1))
            if isinstance(parsed, list):
                for item in parsed:
                    obl = _normalize_obligation(item, article_id, document_name)
                    if obl:
                        obligations.append(obl)
        except Exception:
            pass

    return obligations


def _normalize_obligation(
    raw: dict[str, Any], article_id: str, document_name: str,
) -> dict[str, Any] | None:
    """将 SKILL.md YAML 格式的 dict 标准化为 ClauseObligation 字段。"""
    # 处理 "记录1: { ... }" 嵌套格式
    for key in list(raw.keys()):
        if key.startswith("记录") and isinstance(raw[key], dict):
            raw = raw[key]
            break
        if key.startswith("记录"):
            return _normalize_obligation(raw[key], article_id, document_name)

    def _get(*keys: str, default: Any = "/") -> Any:
        for k in keys:
            if k in raw:
                return raw[k]
        return default

    content = _get("义务内容", default={})
    if isinstance(content, str):
        content = {}
    source = _get("条款出处", default={})
    if isinstance(source, str):
        source = {}

    obligation_rule_str = _get("义务规则", default="应当")
    if isinstance(content, dict):
        obligation_rule_str = content.get("义务规则", obligation_rule_str)

    obligation_verb = _get("义务动作", default="")
    obligation_target = _get("义务事项", default="")
    if isinstance(content, dict):
        obligation_verb = content.get("义务动作", obligation_verb)
        obligation_target = content.get("义务事项", obligation_target)

    source_article = _get("条款编号", default=article_id)
    source_text = _get("条款核心原句", default="")
    if isinstance(source, dict):
        source_article = source.get("条款编号", source_article)
        source_text = source.get("条款核心原句", source_text)

    # 定义/列举类条款不拆分
    should_split = _get("是否拆分", default="是")
    if isinstance(content, dict):
        should_split = content.get("是否拆分", should_split)
    if should_split == "否":
        logger.debug("条款 %s 标记为不拆分，跳过", article_id)
        return None

    settings = get_settings()
    rule_value = obligation_rule_str.strip()
    if rule_value in settings.obligation_shall_words:
        obligation_rule = ObligationRule.SHALL
    elif rule_value in settings.obligation_prohibit_words:
        obligation_rule = ObligationRule.PROHIBIT
    else:
        obligation_rule = ObligationRule.SHALL

    if not obligation_verb or not obligation_target:
        return None

    return {
        "index": 0,
        "trigger_condition": _get("触发义务条件", default="/"),
        "trigger_sub_condition": _get("触发义务子条件", default="/"),
        "subject": _get("义务主体", default="/"),
        "object": _get("义务客体", default="/"),
        "obligation_rule": obligation_rule,
        "action_verb": obligation_verb,
        "action_target": obligation_target,
        "method": _get("实现义务方式", default="/"),
        "document_name": _get("文件名称", default=document_name),
        "clause_number": source_article,
        "source_text": source_text or _get("引用原文", default=""),
        "ambiguity_note": _get("歧义说明", default="无歧义"),
        "ambiguity_detail": _get("歧义详情", default=""),
        "should_split": "是",
    }
