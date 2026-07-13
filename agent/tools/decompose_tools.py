"""条款拆分执行工具 —— 用于 decompose 节点（ReAct）.

核心工具：decompose_article_batch（调用 LLM + SKILL.md 规范进行拆分）。
辅助工具：validate_obligation_schema（Pydantic schema 验证）。
"""

from __future__ import annotations

import json
from typing import Any

from agent.prompts.loader import PromptLoader
from config.settings import get_settings
from models.clause import ClauseObligation, ObligationRule
from utils import get_logger
from utils.file.json_utils import parse_json_from_text

logger = get_logger()


# ── 工具: 批量拆分条款 ─────────────────────────────────────────────────────

def decompose_article_batch(
    articles_json: str,
    document_name: str = "",
    context: str = "",
    llm: Any = None,
) -> dict[str, Any]:
    """将一个或多个条款拆分为原子化义务记录 [核心工具].

    调用 LLM（注入 SKILL.md 规范）对给定条款文本进行义务拆分。
    结果会经过 Pydantic schema 验证。

    Args:
        articles_json: JSON 字符串，格式为 [{"id": "第一条", "text": "完整文本"}, ...]。
        document_name: 文档名称，填入"文件名称"字段。
        context: 前文上下文（用于主体/条件的补全）。
        llm: LLM 实例（由 ToolRegistry.execute 注入）。

    Returns:
        {
            "success": true/false,
            "obligations": [ClauseObligation dict, ...],
            "total_obligations": 整数,
            "validation_errors": [错误列表],
            "message": 说明
        }
    """
    if llm is None:
        return {
            "success": False,
            "obligations": [],
            "total_obligations": 0,
            "validation_errors": ["LLM 实例未注入，无法执行拆分"],
            "message": "llm is None",
        }

    try:
        articles = json.loads(articles_json)
    except json.JSONDecodeError as e:
        return {
            "success": False,
            "obligations": [],
            "total_obligations": 0,
            "validation_errors": [f"articles_json 解析失败: {e}"],
            "message": "invalid JSON",
        }

    if not isinstance(articles, list):
        return {
            "success": False,
            "obligations": [],
            "total_obligations": 0,
            "validation_errors": ["articles_json 必须是数组格式"],
            "message": "not a list",
        }

    # Build decomposition prompt
    loader = PromptLoader()
    system_prompt = loader.get_decompose_simple_prompt(document_name=document_name)

    obligations: list[dict[str, Any]] = []
    errors: list[str] = []

    for article in articles:
        article_id = article.get("id", "未知")
        article_text = article.get("text", "")

        user_prompt = f"""请拆分以下条款：

条款编号：{article_id}
条款文本：{article_text}
"""

        if context:
            user_prompt += f"\n上下文信息（前文已拆分的记录供参考）：\n{context}"

        try:
            # 调用 LLM
            response = llm.invoke([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ])

            # 提取 YAML/JSON 结果
            content = response.content if hasattr(response, "content") else str(response)
            parsed = _parse_llm_output_to_obligations(content, article_id, document_name)

            if parsed:
                obligations.extend(parsed)
            else:
                errors.append(f"条款 {article_id}: LLM 输出无法解析为有效记录")

        except Exception as e:
            logger.error("拆分条款 %s 失败: %s", article_id, e)
            errors.append(f"条款 {article_id}: {e}")

    # Pydantic 验证
    validated: list[dict[str, Any]] = []
    validation_errors: list[str] = []
    for i, obl_dict in enumerate(obligations):
        try:
            obl = ClauseObligation(**obl_dict)
            validated.append(obl.model_dump())
        except Exception as e:
            validation_errors.append(f"第{i + 1}条记录验证失败: {e}")

    return {
        "success": len(validated) > 0,
        "obligations": validated,
        "total_obligations": len(validated),
        "validation_errors": errors + validation_errors,
        "message": f"成功拆分 {len(validated)} 条义务记录"
        + (f"，{len(validation_errors)} 条验证失败" if validation_errors else ""),
    }


# ── 工具: Schema 验证 ──────────────────────────────────────────────────────

def validate_obligation_schema(obligations_json: str) -> dict[str, Any]:
    """验证一组义务记录是否符合 Pydantic schema.

    Args:
        obligations_json: JSON 字符串，义务记录数组。

    Returns:
        {
            "all_valid": true/false,
            "total": 总数,
            "valid_count": 通过数,
            "errors": [{index, message, record_preview}, ...]
        }
    """
    try:
        data = json.loads(obligations_json)
    except json.JSONDecodeError as e:
        return {
            "all_valid": False,
            "total": 0,
            "valid_count": 0,
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
            errors.append({
                "index": i,
                "message": str(e),
                "record_preview": str(record)[:200],
            })

    return {
        "all_valid": len(errors) == 0,
        "total": len(data),
        "valid_count": valid_count,
        "errors": errors,
    }


# ── 辅助: 解析 LLM 输出 ────────────────────────────────────────────────────

def _parse_llm_output_to_obligations(
    content: str, article_id: str, document_name: str,
) -> list[dict[str, Any]]:
    """将 LLM 输出的 YAML/JSON/文本 解析为 ClauseObligation dict 列表."""
    import re
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
    """将 SKILL.md YAML 格式的 dict 标准化为 ClauseObligation 字段."""
    # 处理 "记录1: { ... }" 嵌套格式
    for key in list(raw.keys()):
        if key.startswith("记录") and isinstance(raw[key], dict):
            raw = raw[key]
            break
        # 处理每个 key 都是 "记录N" 的情况（只取第一个）
        if key.startswith("记录"):
            return _normalize_obligation(raw[key], article_id, document_name)

    # Extract fields, try multiple key names for compatibility
    def _get(*keys: str, default: Any = "/") -> Any:
        for k in keys:
            if k in raw:
                return raw[k]
        return default

    # Handle nested 义务内容 / 条款出处 from LLM YAML output
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

    # Convert obligation rule string to enum (using configurable word sets)
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

    # Return flat dict with ENGLISH field names (as Pydantic model expects)
    return {
        "index": 0,
        "trigger_condition": _get("触发义务条件", default="/"),
        "trigger_sub_condition": _get("触发义务子条件", default="/"),
        "subject": _get("义务主体", default=""),
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
    }
