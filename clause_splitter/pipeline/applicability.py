"""Stage 1 — document-level applicability judgment (是否适用于平安科技).

Light text extraction (no MinerU — irrelevant regulations never pay that cost),
one LLM call against ``prompts/applicability.md``. Fail-open: any error ⇒
``applicable=None`` (需人工复核), never silently false — a wrong "no" would
drop the whole regulation from the pipeline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from config.config_app import CONFIG_APP
from clause_splitter.config import LLM_TIMEOUT_S
from clause_splitter.prompts import render
from clause_splitter.utils import get_logger
from clause_splitter.utils.file.doc_parser import extract_text
from ..compare.ground import _norm_str, parse_answer_json
from .contexts import StageContext

logger = get_logger()


def parse_verdict(content: str) -> dict[str, Any] | None:
    """Parse the ``<answer>`` JSON; None when fields are missing / malformed."""
    parsed = parse_answer_json(content)
    if not isinstance(parsed, dict):
        return None
    applicable = parsed.get("applicable")
    if isinstance(applicable, str):
        applicable = applicable.strip().lower() == "true"
    if not isinstance(applicable, bool):
        return None
    try:
        confidence = max(0.0, min(float(parsed.get("confidence") or 0.0), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "applicable": applicable,
        "confidence": round(confidence, 2),
        "reason": _norm_str(parsed.get("reason")),
    }


def build_judge_messages(title: str, text: str, truncated: bool) -> list[dict[str, str]]:
    user = f"【法规名称】{title or '(未命名)'}\n【法规全文】\n{text}"
    if truncated:
        user += "\n\n(注:原文超长,以上仅为截取部分,请基于已有内容判定。)"
    user += "\n\n请按判定框架与输出契约判定,返回 <answer> JSON。"
    return [
        {"role": "system", "content": render("applicability", company_profile=CONFIG_APP.company_profile)},
        {"role": "user", "content": user},
    ]


async def run(ctx: StageContext) -> dict[str, Any]:
    """Judge whole-document applicability; returns regulation field updates."""
    src = Path(ctx.regulation["source_path"])
    max_chars = CONFIG_APP.applicability_max_chars
    text = await asyncio.to_thread(extract_text, src, max_chars=max_chars)
    truncated = len(text) >= max_chars

    def fail_open(reason: str) -> dict[str, Any]:
        return {
            "applicability_applicable": None,
            "applicability_confidence": 0.0,
            "applicability_reason": reason,
            "applicability_truncated": truncated,
        }

    if not text.strip():
        logger.warning("applicability: empty text for %s", ctx.reg_id)
        return fail_open("文档文本抽取为空,无法自动判定,需人工复核。")

    try:
        resp = await asyncio.wait_for(
            ctx.llm.ainvoke(build_judge_messages(ctx.regulation.get("title") or "", text, truncated)),
            timeout=LLM_TIMEOUT_S,
        )
        content = resp.content if hasattr(resp, "content") else str(resp)
        verdict = parse_verdict(content)
    except Exception as e:  # noqa: BLE001 — fail-open to needs-review
        logger.warning("applicability judgment failed (%s): %s", ctx.reg_id, e)
        verdict = None

    if verdict is None:
        return fail_open("适用性判定失败(LLM 异常或输出不可解析),需人工复核。")

    if truncated:
        verdict["reason"] = f"{verdict['reason']}(判定基于截取的前 {max_chars} 字符)" if verdict["reason"] else ""
    return {
        "applicability_applicable": verdict["applicable"],
        "applicability_confidence": verdict["confidence"],
        "applicability_reason": verdict["reason"],
        "applicability_truncated": truncated,
    }
