"""差距分析报告生成 —— 子 agent 分 batch 总结 + skills/summary/SKILL.md + LLM。

报告是比对的最后产物。``generate_gap_report`` 先让多个「子 agent」(每次
``llm.invoke``,无工具)分 batch 总结比对结果 —— 每次调用只吃该批条款,单次
输入远小于上下文窗口;再由最终模型按 ``skills/summary/SKILL.md`` 拿全部批次
总结 + 确定性统计撰写正式报告(语气专业、证据引用承接内规原文)。LLM 失败或
未注入时,回退到确定性的 ``_template_report`` —— 纯模板拼装,统计表 + 全部
MatchResult 逐行呈现,证据原文直接引用,绝不空谈。

两种路径都产出完整 Markdown,写入 ``{output_dir}/内外规合规差距分析报告.md``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clause_splitter.config import REPORT_BATCH_SIZE, REPO_ROOT
from clause_splitter.models import CompareResult, CoverageStatus, MatchResult
from clause_splitter.utils import get_logger
from clause_splitter.utils.skill_loader import load_skill
from ..tools.searcher.store import DocumentStore

logger = get_logger()

_REPORT_FILENAME = "内外规合规差距分析报告.md"
_SKILLS_DIR = REPO_ROOT / "clause_splitter" / "skills"

#: 模板回退用状态中文映射(status 枚举值本身已是中文,直接复用)。
_STATUS_LABEL = {
    CoverageStatus.FULLY_COVERED: "完全覆盖",
    CoverageStatus.PARTIALLY_COVERED: "部分覆盖",
    CoverageStatus.NOT_COVERED: "缺失",
}


# ═══════════════════════════════════════════════════════════════════════
# LLM 报告 —— 子 agent 分 batch 总结,最终模型汇总
# ═══════════════════════════════════════════════════════════════════════

#: 批总结子 agent 的 system prompt:只产出该批条款的紧凑素材,不写整篇报告框架。
_BATCH_SUMMARY_SYSTEM = (
    "你是一名合规比对助手。你会收到一批「外规条款 vs 内规条款」的比对结果"
    "(每条含 external_source_text / status / matched_internal_document_name / "
    "matched_internal_clause_number / matched_internal_source_text / analysis / remedy_suggestion)。\n"
    "请为这一批条款输出紧凑的中文总结,作为后续报告撰写者的素材:\n"
    "- 逐条列出:外规出处+条款号 → 覆盖状态(完全覆盖/部分覆盖/缺失) → 承接内规"
    "(制度名+条款号;缺失写「无」) → 一句话差距/整改建议。\n"
    "- 凡被承接(完全/部分覆盖)的条目,必须用「>」引用 matched_internal_source_text 的关键句作证据,"
    "证明承接成立;缺失的条目说明缺失原因。\n"
    "- 只输出该批素材,不要输出报告标题/摘要/统计表/结论等整篇报告框架,不要重复外规原文全文。\n"
    "- 保持精炼,每条尽量控制在 1-3 行。"
)


def _build_external_clause_md(matches: list[MatchResult]) -> str:
    """外规条款原文(markdown),供批总结子 agent 引用。"""
    lines = []
    for m in matches:
        lines.append(f"### {m.external_clause_id}")
        lines.append(f"出处: {m.external_document_name} {m.external_article_id or ''}".rstrip())
        lines.append(f"原文:\n\n{m.external_source_text}")
        lines.append("")
    return "\n".join(lines)


def _build_matched_internal_md(matches: list[MatchResult]) -> str:
    """只列该批 grounding 实际承接的内规条款(按 id 去重),不喂整个内规库。"""
    seen: set[str] = set()
    lines: list[str] = []
    for m in matches:
        cid = m.matched_internal_id
        if not cid or cid in seen:
            continue
        seen.add(cid)
        lines.append(f"### {cid}")
        if m.matched_internal_clause_number:
            lines.append(f"章节: {m.matched_internal_clause_number}")
        lines.append(f"原文:\n\n{m.matched_internal_source_text}")
        lines.append("")
    if not lines:
        return "(本批无被承接的内规条款)"
    return "\n".join(lines)


def _batch_summary_dict(result: CompareResult, batch: list[MatchResult]) -> dict:
    """单批摘要输入:与 to_summary_dict 同字段,但只含该批条款,且不带复核页候选全文。"""
    return {
        "external_documents": result.external_documents,
        "internal_documents": result.internal_documents,
        "total_external_obligations": len(batch),
        "total_internal_obligations": result.total_internal_obligations,
        "details": [m.model_dump(exclude={"retrieved_candidates"}) for m in batch],
    }


def _batches(rows: list, size: int) -> list[list]:
    """把 matches 按 size 分块。"""
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def _build_batch_prompt(result: CompareResult, batch: list[MatchResult]) -> str:
    """单个批总结子 agent 的 user prompt:只装该批条款。"""
    return (
        "{clause_match_results}\n\n"
        "### 外部规定条款(本批)\n\n"
        "{external_clause_md}\n\n"
        "### 内部规章制度(本批承接条款)\n\n"
        "{internal_clause_md}\n"
    ).format(
        clause_match_results=json.dumps(
            _batch_summary_dict(result, batch), ensure_ascii=False, indent=2
        ),
        external_clause_md=_build_external_clause_md(batch),
        internal_clause_md=_build_matched_internal_md(batch),
    )


def _build_final_prompt(result: CompareResult, summaries: list[str]) -> str:
    """最终模型 user prompt:确定性统计 + 各批总结(不含原始 matches / 候选 / 整库)。"""
    stats = (
        f"- 外规制度:{', '.join(result.external_documents) or '—'}\n"
        f"- 内规制度:{', '.join(result.internal_documents) or '—'}\n"
        f"- 外规条款共 {result.total_external_obligations} 条:完全覆盖 {result.fully_covered_count} 条、"
        f"部分覆盖 {result.partially_covered_count} 条、缺失 {result.not_covered_count} 条。"
    )
    sections = "\n\n".join(
        f"## 批次 {i + 1} 总结\n\n{s}" for i, s in enumerate(summaries)
    )
    return (
        "以下是该次内外规比对的整体统计与各批次总结。请据此按 Skill 的结构撰写完整报告,"
        "凡被承接(完全/部分覆盖)的条目必须引用批次总结中的承接内规原文证据。\n\n"
        "### 整体统计(确定性)\n\n"
        f"{stats}\n\n"
        "### 各批次总结\n\n"
        f"{sections}\n"
    )


# ═══════════════════════════════════════════════════════════════════════
# 模板回退报告(确定性)
# ═══════════════════════════════════════════════════════════════════════


def _internal_ref(m) -> str:
    """承接内规的出处标注:制度名 + 条款号。"""
    if not m.matched_internal_document_name:
        return "(无)"
    return f"{m.matched_internal_document_name} {m.matched_internal_clause_number}".strip()


def _stats_table(result: CompareResult) -> str:
    n = max(len(result.matches), 1)
    rows = [
        ("完全覆盖", result.fully_covered_count, "内规已完整承接"),
        ("部分覆盖", result.partially_covered_count, "需补足子要求或收紧约束"),
        ("缺失", result.not_covered_count, "无制度承接，风险最高"),
    ]
    lines = ["| 覆盖状态 | 条款数 | 占比 | 说明 |", "|---|---:|---:|---|"]
    for label, count, note in rows:
        lines.append(f"| {label} | {count} | {count / n:.1%} | {note} |")
    return "\n".join(lines)


def _covered_section(result: CompareResult) -> str:
    rows = [m for m in result.matches if m.status == CoverageStatus.FULLY_COVERED]
    if not rows:
        return "本部分无完全承接条款。"
    lines = [
        "| 外规条款 | 外规要求（原文摘要） | 承接的内规 | 说明 |",
        "|---|---|---|---|",
    ]
    for m in rows:
        internal = f"{_internal_ref(m)}"
        source = m.matched_internal_source_text or ""
        lines.append(
            f"| {m.external_clause_id} | {m.external_source_text[:60]}… | {internal} | {m.analysis[:80]} |"
        )
        if source:
            lines.append(f"> 承接原文：{source}")
    return "\n".join(lines)


def _partial_section(result: CompareResult) -> str:
    rows = [m for m in result.matches if m.status == CoverageStatus.PARTIALLY_COVERED]
    if not rows:
        return "本部分无部分承接条款。"
    lines = [
        "| 外规条款 | 外规要求 | 部分承接的内规 | 差距点 | 补足建议 |",
        "|---|---|---|---|---|",
    ]
    for m in rows:
        lines.append(
            f"| {m.external_clause_id} | {m.external_source_text[:50]}… | "
            f"{_internal_ref(m)} | {m.analysis[:70]} | {m.remedy_suggestion or '—'} |"
        )
        if m.matched_internal_source_text:
            lines.append(f"> 承接原文：{m.matched_internal_source_text}")
    return "\n".join(lines)


def _missing_section(result: CompareResult) -> str:
    rows = [m for m in result.matches if m.status == CoverageStatus.NOT_COVERED]
    if not rows:
        return "本部分无缺失条款（全部承接）。"
    lines = [
        "| 外规条款 | 外规要求（原文） | 缺失原因 | 整改建议 |",
        "|---|---|---|---|",
    ]
    for m in rows:
        lines.append(
            f"| {m.external_clause_id} | {m.external_source_text[:50]}… | "
            f"{m.analysis[:60]} | {m.remedy_suggestion or '—'} |"
        )
    return "\n".join(lines)


def _priority_actions(result: CompareResult) -> str:
    """结论与行动建议:把部分覆盖 + 缺失的 remedy 按状态优先级排。"""
    items = []
    for m in result.matches:
        if m.status == CoverageStatus.NOT_COVERED:
            items.append((1, m, "高"))
        elif m.status == CoverageStatus.PARTIALLY_COVERED:
            items.append((2, m, "中"))
    if not items:
        return "本报告无整改行动项。"
    items.sort(key=lambda t: t[0])
    lines = ["| 优先级 | 外规条款 | 整改建议 |", "|---|---|---|"]
    for _, m, prio in items:
        lines.append(f"| {prio} | {m.external_clause_id} | {m.remedy_suggestion or '—'} |")
    return "\n".join(lines)


def _template_report(result: CompareResult) -> str:
    """确定性回退报告:统计表 + 三节明细 + 行动清单,证据原文直接引用。"""
    n = len(result.matches)
    scope = (
        f"分析范围：共 {n} 条外规条款，涉及 {len(result.external_documents)} 个外规制度、"
        f"{len(result.internal_documents)} 个内规制度。"
    )
    top_risks = [m for m in result.matches if m.status == CoverageStatus.NOT_COVERED][:3]
    risk_lines = "\n".join(
        f"- **{m.external_clause_id}**（{m.external_document_name}）：{m.analysis[:60]}"
        for m in top_risks
    ) or "- 无完全缺失条款。"

    return f"""# 内外规合规差距深度分析报告

## 摘要

- **{scope}**
- **关键风险点**：
{risk_lines}

## 覆盖情况总览

{_stats_table(result)}

## 三、完全承接的条款

{_covered_section(result)}

## 四、部分承接需补足的条款

{_partial_section(result)}

## 五、缺失的条款

{_missing_section(result)}

## 结论与行动建议

{_priority_actions(result)}

---
*本报告由模板自动生成(LLM 报告不可用时的确定性回退)。证据原文直接引用自比对结果。*
"""


# ═══════════════════════════════════════════════════════════════════════
# 对外入口
# ═══════════════════════════════════════════════════════════════════════


def write_report(report: str, output_dir: str | Path) -> Path:
    """把报告写入 ``{output_dir}/内外规合规差距分析报告.md``。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / _REPORT_FILENAME
    path.write_text(report, encoding="utf-8")
    logger.info("差距分析报告已写入: %s", path)
    return path


def generate_gap_report(
    result: CompareResult,
    store: DocumentStore,
    llm: Any = None,
    *,
    output_dir: str | Path = "",
) -> dict[str, Any]:
    """生成差距分析报告(LLM 优先,模板回退)。

    Args:
        result: 比对结果。
        store: 内规条款库 —— 兼容参数,批处理路径按批喂承接条款,不再喂整库全文。
        llm: ``LLM`` facade;None 或调用失败时用模板回退。
        output_dir: 非空则把报告写入该目录。

    Returns:
        {"report": str, "report_path": str, "mode": "llm" | "template"}
    """
    report = ""
    mode = "template"

    if llm is not None:
        try:
            skill = load_skill(_SKILLS_DIR / "summary" / "SKILL.md")
            skill = skill.replace("{datetime}", __import__("datetime").date.today().isoformat())
            # 子 agent 分 batch 总结:每次调用只吃该批条款,单次输入远小于上下文窗口。
            summaries: list[str] = []
            for i, batch in enumerate(_batches(result.matches, REPORT_BATCH_SIZE), 1):
                bresp = llm.invoke([
                    {"role": "system", "content": _BATCH_SUMMARY_SYSTEM},
                    {"role": "user", "content": _build_batch_prompt(result, batch)},
                ])
                btext = bresp.content if hasattr(bresp, "content") else str(bresp)
                if not btext or not btext.strip():
                    raise ValueError(f"批次 {i} 总结为空")
                summaries.append(btext.strip())
            # 最终模型拿批次总结 + 确定性统计写完整报告。
            resp = llm.invoke([
                {"role": "system", "content": skill},
                {"role": "user", "content": _build_final_prompt(result, summaries)},
            ])
            content = resp.content if hasattr(resp, "content") else str(resp)
            if content and content.strip():
                report = content.strip()
                mode = "llm"
        except Exception as e:  # noqa: BLE001 — LLM 失败不阻塞产物
            logger.warning("LLM 报告生成失败,回退模板: %s", e)

    if not report:
        report = _template_report(result)
        mode = "template"

    report_path = str(write_report(report, output_dir)) if output_dir else ""
    return {"report": report, "report_path": report_path, "mode": mode}
