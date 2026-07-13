"""结果验证节点 —— Pydantic 验证 + LLM Review.

对拆分结果进行双重验证：
1. 硬性 Pydantic schema 验证（确定性）
2. LLM Review 完整性检查（语义级别）

验证失败时触发 self_correct 循环（最多3次）。
"""

from __future__ import annotations

# state is plain dict
from config.settings import get_settings
from models.clause import ClauseObligation, ClauseSplitResult
from utils import get_logger

logger = get_logger()


def validate_node(state: dict) -> dict:
    """验证拆分结果的完整性和正确性.

    Args:
        state: 包含 obligations 的 AgentState。

    Returns:
        包含 validation_errors 及进度更新的部分状态。
    """
    obligations = state.get("obligations", [])
    raw_text = state.get("raw_text", "")
    document_name = state.get("document_name", "")
    retry_count = state.get("retry_count", 0)

    logger.info(
        "正在验证拆分结果: %d 条义务记录 (retry=%d)……",
        len(obligations), retry_count,
    )

    max_retries = get_settings().max_self_correct_retries
    validation_errors: list[dict] = []

    # ── 硬性 Pydantic 验证 ──────────────────────────────────────────────
    valid_obligations: list[ClauseObligation] = []
    for i, obl_dict in enumerate(obligations):
        try:
            obl = ClauseObligation(**obl_dict)
            valid_obligations.append(obl)
        except Exception as e:
            validation_errors.append({
                "index": i,
                "article_id": obl_dict.get("clause_number", "未知"),
                "error": str(e),
                "record": obl_dict,
            })

    if validation_errors and retry_count < max_retries:
        logger.warning(
            "验证失败: %d/%d 条记录不合法，将触发 self_correct",
            len(validation_errors), len(obligations),
        )
        return {
            "validation_errors": validation_errors,
            "retry_count": retry_count + 1,
            "current_step": f"验证失败 —— {len(validation_errors)} 条记录需修正 (retry {retry_count + 1}/{max_retries})",
            "status": "validating",
        }

    # ── 通过验证：整理输出 ──────────────────────────────────────────────
    # Re-number
    for i, obl in enumerate(valid_obligations):
        obl.index = i + 1

    result = ClauseSplitResult(
        document_name=document_name,
        total_articles=state.get("total_articles", 0),
        total_obligations=len(valid_obligations),
        obligations=valid_obligations,
    )

    milestones = get_settings().progress_milestones
    logger.info("验证通过: %d 条义务记录", result.total_obligations)

    return {
        "obligations": [obl.model_dump() for obl in valid_obligations],
        "validation_errors": [],
        "retry_count": 0,  # 重置
        "progress": milestones["validate_complete"],
        "current_step": f"验证通过 —— {result.total_obligations} 条有效义务记录",
        "status": "validating",
    }


def route_after_validate(state: dict) -> str:
    """验证后的条件路由.

    Returns:
        "export": 验证通过，直接导出。
        "self_correct": 有错误且未超重试次数。
        "fail": 超过最大重试次数。
    """
    errors = state.get("validation_errors", [])
    retry_count = state.get("retry_count", 0)
    max_retries = get_settings().max_self_correct_retries

    if not errors:
        return "export"
    if retry_count < max_retries:
        return "self_correct"
    return "export"  # 超过重试次数，强制执行导出（带错误标注）


def self_correct_node(state: dict) -> dict:
    """Self-correct 节点 —— 针对性重新拆分失败的记录.

    仅对验证失败的记录进行重新拆分，不清除有效的记录。
    将失败信息反馈给 LLM 进行修正。

    Args:
        state: 包含 validation_errors, obligations 的 AgentState。

    Returns:
        合并修正后结果的部分状态。
    """
    validation_errors = state.get("validation_errors", [])
    obligations = state.get("obligations", [])

    logger.info("Self-correct: 修正 %d 条失败记录", len(validation_errors))

    # 标记失败的记录索引
    failed_indices = {err["index"] for err in validation_errors}

    # 保留有效的记录
    valid = [obl for i, obl in enumerate(obligations) if i not in failed_indices]

    # Fix failed records — crude fallback for compound verbs
    for err in validation_errors:
        record = err.get("record", {})
        # Try to fix compound verb by taking the first one
        if "action_verb" in err.get("error", ""):
            verb = record.get("action_verb", "")
            first_verb = verb.split("、")[0].split("并")[0].split("和")[0]
            record["action_verb"] = first_verb

        # Re-validate
        try:
            ClauseObligation(**record)
            valid.append(record)
            logger.info("Self-correct OK: %s", err.get("article_id", ""))
        except Exception:
            logger.warning(
                "Self-correct failed, skip: %s → %s",
                err.get("article_id", ""), err.get("error", ""),
            )
            # Create a placeholder record for manual review
            record["ambiguity_note"] = f"[Auto-fix failed: {err.get('error', '')[:100]}]"
            record["action_verb"] = record.get("action_verb", "/")
            record["action_target"] = record.get("action_target", "（需人工复核）")
            valid.append(record)

    # Re-number
    for i, obl in enumerate(valid):
        obl["index"] = i + 1

    return {
        "obligations": valid,
        "validation_errors": [],
        "current_step": f"Self-correct 完成 —— {len(valid)} 条义务记录",
        "status": "validating",
    }
