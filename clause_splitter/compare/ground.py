"""Grounding —— 把 agent 判定落到可核实的 MatchResult,防幻觉.

历史教训:"批量 0% 覆盖" bug —— agent 声称已覆盖,但引用的内规 ID 不在
检索结果里。三道防线:
1. 答案契约只允许引用轨迹内 chunk_id(agent/prompts.py 铁律)。
2. ``ground_episode`` 硬校验:每个 claimed id 必须 ∈ seen 集 且 ∈ store。
   不落地的 claim 一律去掉。
3. 覆盖性判定(完全覆盖/部分覆盖)若没有任何可接地 claim → 降级为 缺失,
   analysis 整体替换为降级文案,防止编造的引用混进差距报告。
"""

from __future__ import annotations

import json
import re
from typing import Any

from clause_splitter.agent import AgentResult, AgentTurn
from clause_splitter.models import CoverageStatus, normalize_internalization_decision
from clause_splitter.utils import get_logger
from .episode import ExternalClause
from ..tools.searcher.store import DocumentStore

logger = get_logger()

_DOWNGRADE_ANALYSIS = (
    "内规库中未检索到可承接的候选条款。判定声称已覆盖，但引用的内规条款 ID "
    "无法对应到任何检索结果（疑似幻觉），已降级为缺失。请人工复核该外规义务是否确有内规承接。"
)
#: agent 未给出分析文案时的缺省说明(MatchResult.analysis 要求非空)。
_NOT_COVERED_ANALYSIS = "内规库中未检索到承接该外规义务的条款，判定为缺失。"

_ANSWER_RE = re.compile(r"<answer>(.*?)</answer>", re.DOTALL)


def parse_answer_json(raw_answer: str) -> dict[str, Any] | None:
    """从 agent 最终消息的 ``<answer>…</answer>`` 中提取 JSON。

    提取失败返回 None(由调用方决定降级)。
    """
    m = _ANSWER_RE.search(raw_answer)
    body = m.group(1).strip() if m else raw_answer.strip()
    if not body:
        return None
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def collect_seen_chunk_ids(trajectory: list[AgentTurn]) -> set[str]:
    """Union 所有 ``*_search`` / ``get_context`` 工具结果里的 chunk_id。

    这是 grounding 的候选全集 —— 任何未被工具返回过的 chunk_id 一律视为幻觉。
    """
    seen: set[str] = set()
    for turn in trajectory:
        if not turn.tool_name.endswith("_search") and turn.tool_name != "get_context":
            continue
        try:
            rows = json.loads(turn.tool_result)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("chunk_id"):
                seen.add(str(row["chunk_id"]))
    return seen


def ground_episode(
    result: AgentResult,
    store: DocumentStore,
    ext: ExternalClause,
) -> dict[str, Any]:
    """把一条 episode 的 AgentResult 转成可核实的 MatchResult dict。

    Args:
        result: 已完成的 episode。
        store: 内规条款库(唯一文本来源)。
        ext: 外规条款(回填外规出处与原文)。

    Returns:
        MatchResult 字段 dict。覆盖性 claim 无法落地时降级为 缺失。
    """
    # agent 若配置了 answer_parser=parse_answer_json,parsed_answer 已是 dict,
    # 直接用;否则回退到从 raw_answer 提取。
    parsed = result.parsed_answer if isinstance(result.parsed_answer, dict) else parse_answer_json(result.raw_answer)
    status = _norm_status(parsed.get("status") if parsed else None)
    claimed_ids = _norm_claimed_ids(parsed.get("matched_internal_clause_ids") if parsed else None)
    analysis = str(parsed.get("analysis") or "") if parsed else ""

    base = {
        "external_clause_id": ext.clause_id,
        "external_document_name": ext.document_name,
        "external_article_id": ext.clause_number,
        "external_source_text": ext.full_text,
        "status": status,
        "matched_internal_id": None,
        "matched_internal_document_name": None,
        "matched_internal_clause_number": "",
        "matched_internal_source_text": "",
        "analysis": analysis,
        # 内化判定由**前置内化判定阶段**(检索前)写入 ext,episode 不再产出;
        # 缺失/非 str 一律回空串,不阻塞比对;旧数据「预防性内化」两值化为「需要内化」。
        "clause_type": _norm_str(ext.clause_type),
        "internalization_decision": normalize_internalization_decision(_norm_str(ext.internalization_decision)),
        "internalization_reason": _norm_str(ext.internalization_reason),
        # difference_analysis / core_regulatory_requirements 仍由 episode 产出
        # (差异说明依赖比对结果;核心监管要求为外规义务提取)。
        "difference_analysis": _norm_str(parsed.get("difference_analysis") if parsed else ""),
        "core_regulatory_requirements": _norm_str(parsed.get("core_regulatory_requirements") if parsed else ""),
    }

    # 缺失:无承接 —— 直接返回(claimed ids 即使有也不落地)。
    if status == CoverageStatus.NOT_COVERED:
        if not base["analysis"].strip():
            base["analysis"] = _NOT_COVERED_ANALYSIS
        return base

    # 覆盖性判定 → 必须 grounding。
    seen = collect_seen_chunk_ids(result.trajectory)
    grounded: list[str] = []
    evidence: dict[str, str] = {}
    for cid in claimed_ids:
        # 双硬校验:∈ seen 集 且 ∈ store。任一不过即丢弃该 claim。
        if cid not in seen:
            logger.warning("claim %r 不在检索轨迹内,丢弃", cid)
            continue
        chunk = store.get_chunk(cid)
        if chunk is None:
            logger.warning("claim %r 不在 store 中,丢弃", cid)
            continue
        grounded.append(cid)
        # evidence 以 store 原文为准,不回读 agent 输出的改写文本。
        ev = None
        if parsed:
            ev = parsed.get("evidence", {}).get(cid)
        evidence[cid] = str(ev or chunk.text[:200])

    if not grounded:
        logger.warning(
            "ext=%s status=%s 但无可接地 claim,降级为缺失",
            ext.clause_id, status,
        )
        return {
            **base,
            "status": CoverageStatus.NOT_COVERED,
            "analysis": _DOWNGRADE_ANALYSIS,
        }

    # 用第一承接条款回填 MatchResult 的内规侧字段(报告展示用);全量承接
    # 列表存 matched_internal_clause_ids(复核页给所有 adopted 候选打「✓ 采用」)。
    # 内规 chunk 来自 mineru 层级分块:制度名取 chunk.doc_id,条款/段落标识
    # 取 section 路径的末级标题(如 "第二十条"),无标题文档回退到序号。
    head = store.get_chunk(grounded[0])
    return {
        **base,
        "status": status,
        "matched_internal_id": grounded[0],
        "matched_internal_document_name": head.doc_id if head else None,
        "matched_internal_clause_number": _segment_label(head) if head else "",
        "matched_internal_source_text": head.text if head else "",
        "matched_internal_clause_ids": grounded,
        "analysis": _merge_analysis(analysis, grounded, store),
    }


# ── 归一化辅助 ─────────────────────────────────────────────────────────


def _norm_status(value: Any) -> CoverageStatus:
    """把 agent 输出的状态串归一到 CoverageStatus 枚举。"""
    if value is None:
        return CoverageStatus.NOT_COVERED
    s = str(value).strip()
    for status in CoverageStatus:
        if s in (status.value, status.name):
            return status
    logger.warning("未知覆盖状态 %r,按缺失处理", value)
    return CoverageStatus.NOT_COVERED


def _norm_claimed_ids(value: Any) -> list[str]:
    """归一化 claimed ids(兼容逗号分隔串 / 数组)。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def _norm_str(value: Any) -> str:
    """宽容取字符串:缺失 / 非 str 一律回空串,避免新字段拖垮比对。"""
    if isinstance(value, str):
        return value.strip()
    return ""


def _segment_label(chunk) -> str:
    """内规 chunk 的条款/段落标识。

    优先取 ``meta.section`` 的末级标题(如 "第三章 数据安全 > 第二十条" → "第二十条";
    Excel 内规库 ``section`` 直接是 "第一条" 也直接返回,不再掉进 0 起始序号回退)。
    无 section 时,从 chunk 原文开头提取 "第X条"(文本通常以条款号起头);
    仍无则用 1 起始的段落序号(``第N段``),与实际的 1 起始编号对齐。
    """
    section = str(chunk.meta.get("section", "") or "").strip() if chunk.meta else ""
    if section:
        leaf = section.split(" > ")[-1].strip()
        if leaf:
            return leaf
    head = re.match(r"^\s*(第[一二三四五六七八九十百零\d]+条)", chunk.text or "")
    if head:
        return head.group(1)
    if "#" in chunk.chunk_id:
        order = chunk.chunk_id.rsplit("#", 1)[-1]
        if order.isdigit():
            return f"第{int(order) + 1}段"
    return ""


def _merge_analysis(analysis: str, grounded: list[str], store: DocumentStore) -> str:
    """analysis 为空时用承接条款兜底(复核页/报告的判定分析列要求非空)。

    analysis **非空时不再追加**「承接内规条款: …」——该信息由复核页「✓ 采用内规」、
    报告的「承接内规」列单独展示,追加只会让判定分析段落变长、与简洁要求冲突。
    兜底时每个 grounded chunk_id 转成 ``制度名 第X条``(走 ``_segment_label`` 与
    复核页同一套 1 起始编号),避免 0 起始的 chunk_id(如 ``…#0``)泄漏。
    """
    if analysis:
        return analysis
    refs = []
    for cid in grounded:
        chunk = store.get_chunk(cid)
        doc = chunk.doc_id if chunk else ""
        label = _segment_label(chunk) if chunk else ""
        refs.append(f"{doc} {label}".strip() or cid)
    return "承接内规条款: " + ", ".join(refs)
