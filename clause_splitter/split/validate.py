"""拆分结果验证 —— Pydantic 硬校验 + self-correct 循环.

验证失败时针对性修正失败记录(不清除有效记录),最多重试
``max_self_correct_retries`` 次;超限后强制导出(带歧义标注,人工复核)。
"""

from __future__ import annotations

from typing import Any

from clause_splitter.config import get_settings
from clause_splitter.models import ClauseObligation
from clause_splitter.utils import get_logger

logger = get_logger()


def validate(obligations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pydantic 校验义务记录,返回 (有效列表, 错误列表)。

    有效的记录会被重新编号(index 1..N)。
    """
    valid: list[ClauseObligation] = []
    errors: list[dict[str, Any]] = []

    for i, obl_dict in enumerate(obligations):
        try:
            valid.append(ClauseObligation(**obl_dict))
        except Exception as e:
            errors.append({
                "index": i,
                "article_id": obl_dict.get("clause_number", "未知"),
                "error": str(e),
                "record": obl_dict,
            })

    for i, obl in enumerate(valid):
        obl.index = i + 1

    return [obl.model_dump() for obl in valid], errors


def self_correct(
    obligations: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """针对性修正验证失败的记录。

    1. 保留有效记录
    2. 对失败记录做粗糙修复(复合动词取第一个)
    3. 仍失败的记录 → 占位记录(歧义说明标注 [Auto-fix failed], 供人工复核)
    """
    failed_indices = {err["index"] for err in errors}
    valid = [obl for i, obl in enumerate(obligations) if i not in failed_indices]

    for err in errors:
        record = dict(err.get("record", {}))

        # 复合动词修复:取第一个
        if "action_verb" in err.get("error", ""):
            verb = record.get("action_verb", "")
            first_verb = str(verb).split("、")[0].split("并")[0].split("和")[0]
            record["action_verb"] = first_verb

        try:
            ClauseObligation(**record)
            valid.append(record)
            logger.info("Self-correct OK: %s", err.get("article_id", ""))
        except Exception:
            logger.warning(
                "Self-correct failed, skip: %s → %s",
                err.get("article_id", ""), err.get("error", ""),
            )
            record["ambiguity_note"] = f"[Auto-fix failed: {str(err.get('error', ''))[:100]}]"
            record["action_verb"] = record.get("action_verb", "/")
            record["action_target"] = record.get("action_target", "（需人工复核）")
            record.setdefault("should_split", "是")
            valid.append(record)

    for i, obl in enumerate(valid):
        obl["index"] = i + 1

    return valid


def validate_with_retry(
    obligations: list[dict[str, Any]],
    max_retries: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """validate → 有错且未超限 → self_correct → 再 validate,循环至多 max_retries 次。

    Returns:
        (final_obligations, final_errors)。若重试耗尽仍失败,错误记录已转占位,
        不会导致流水线失败。
    """
    settings = get_settings()
    max_retries = settings.max_self_correct_retries if max_retries is None else max_retries

    current = list(obligations)
    for attempt in range(max_retries + 1):
        valid, errors = validate(current)
        if not errors:
            logger.info("验证通过: %d 条义务记录", len(valid))
            return valid, []
        if attempt >= max_retries:
            logger.warning(
                "验证超限: 仍剩 %d 条失败记录,将强制导出(带标注)",
                len(errors),
            )
            return valid, errors
        logger.warning(
            "验证失败: %d 条记录不合法,self-correct (retry %d/%d)",
            len(errors), attempt + 1, max_retries,
        )
        current = self_correct(current, errors)

    return current, []
