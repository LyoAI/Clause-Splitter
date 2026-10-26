"""差距分析报告生成 —— 分批总结 + 汇总撰写,LLM 失败时回退模板。

两种路径都产出 Markdown,写入 ``{output_dir}/内外规合规差距分析报告.md``。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from clause_splitter.config import REPORT_BATCH_SIZE
from clause_splitter.models import (
    CompareResult,
    CoverageStatus,
    MatchResult,
    normalize_internalization_decision,
)
from clause_splitter.prompts import load, render
from clause_splitter.utils import get_logger

logger = get_logger()

_REPORT_FILENAME = "内外规合规差距分析报告.md"

#: 模板回退用状态中文映射(status 枚举值本身已是中文,直接复用)。
_STATUS_LABEL = {
    CoverageStatus.FULLY_COVERED: "完全覆盖",
    CoverageStatus.PARTIALLY_COVERED: "部分覆盖",
    CoverageStatus.NOT_COVERED: "缺失",
}

#: 批总结子 agent 的 system prompt:只产出该批条款的紧凑素材,不写整篇报告框架。
_BATCH_SUMMARY_SYSTEM = load("report_batch")


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
        f"- 外规条款共 {result.total_external_obligations} 条,需内化处理 {result.internalizable_count} 条"
        f"(完全覆盖 {result.fully_covered_count} 条、部分覆盖 {result.partially_covered_count} 条、"
        f"缺失 {result.internalizable_missing_count} 条);{result.no_internalization_count} 条判定无需内化,"
        f"不纳入覆盖统计。"
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


def _internal_ref(m) -> str:
    """承接内规的出处标注:制度名 + 条款号。"""
    if not m.matched_internal_document_name:
        return "(无)"
    return f"{m.matched_internal_document_name} {m.matched_internal_clause_number}".strip()


def _stats_table(result: CompareResult) -> str:
    """覆盖情况总览 —— 分母为需内化处理条款数(不含"无需内化")。"""
    n = max(result.internalizable_count, 1)
    rows = [
        ("完全覆盖", result.fully_covered_count, "内规已完整承接"),
        ("部分覆盖", result.partially_covered_count, "需补足子要求或收紧约束"),
        ("缺失", result.internalizable_missing_count, "无制度承接，风险最高"),
    ]
    lines = ["| 覆盖状态 | 条款数 | 占比 | 说明 |", "|---|---:|---:|---|"]
    for label, count, note in rows:
        lines.append(f"| {label} | {count} | {count / n:.1%} | {note} |")
    if result.no_internalization_count:
        lines.append(
            f"\n> 共 {len(result.matches)} 条外规条款,其中 {result.no_internalization_count} 条判定"
            "**无需内化**(宣示性/程序性等),不纳入覆盖率统计;上表按需内化处理的 "
            f"{result.internalizable_count} 条统计。"
        )
    return "\n".join(lines)

def _internalization_overview(result: CompareResult) -> str:
    """内化判定概览:需要内化 / 无需内化 / 待判定 各几条(二值,预防性并入需要内化)。"""
    if not result.matches:
        return "本报告无内化判定数据。"
    total = len(result.matches)
    counts: dict[str, int] = {}
    for m in result.matches:
        key = normalize_internalization_decision((m.internalization_decision or "").strip()) or "待判定"
        counts[key] = counts.get(key, 0) + 1
    notes = {
        "需要内化": "义务性条款、指导性/金融科技特殊领域（预防性内化），需新建/修订制度",
        "无需内化": "宣示性/程序性条款或已有制度充分覆盖",
        "待判定": "未给出内化判定，需人工复核",
    }
    lines = ["| 内化判定 | 条款数 | 占比 | 说明 |", "|---|---:|---:|---|"]
    for decision in ("需要内化", "无需内化", "待判定"):
        n = counts.get(decision, 0)
        if n:
            lines.append(f"| {decision} | {n} | {n / total:.1%} | {notes[decision]} |")
    return "\n".join(lines)

def _intl(m) -> str:
    """单条款的内化判定列值(空 → 待判定)。"""
    return (m.internalization_decision or "待判定").strip()

def _covered_section(result: CompareResult) -> str:
    rows = [m for m in result.matches if m.status == CoverageStatus.FULLY_COVERED]
    if not rows:
        return "本部分无完全承接条款。"
    lines = [
        "| 外规条款 | 外规要求（原文摘要） | 承接的内规 | 内化判定 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for m in rows:
        internal = f"{_internal_ref(m)}"
        source = m.matched_internal_source_text or ""
        lines.append(
            f"| {m.external_clause_id} | {m.external_source_text[:60]}… | {internal} | "
            f"{_intl(m)} | {m.analysis[:80]} |"
        )
        if source:
            lines.append(f"> 承接原文：{source}")
    return "\n".join(lines)

def _partial_section(result: CompareResult) -> str:
    rows = [m for m in result.matches if m.status == CoverageStatus.PARTIALLY_COVERED]
    if not rows:
        return "本部分无部分承接条款。"
    lines = [
        "| 外规条款 | 外规要求 | 部分承接的内规 | 内化判定 | 差异说明 |",
        "|---|---|---|---|---|",
    ]
    for m in rows:
        diff = (m.difference_analysis or "—").replace("\n", " ")
        lines.append(
            f"| {m.external_clause_id} | {m.external_source_text[:50]}… | "
            f"{_internal_ref(m)} | {_intl(m)} | {diff[:80]} |"
        )
        if m.matched_internal_source_text:
            lines.append(f"> 承接原文：{m.matched_internal_source_text}")
    return "\n".join(lines)

def _missing_section(result: CompareResult) -> str:
    """缺失的需内化条款(真实制度缺口);"无需内化"单列说明,不当作缺口。"""
    rows = [
        m for m in result.matches
        if m.status == CoverageStatus.NOT_COVERED
        and (m.internalization_decision or "").strip() != "无需内化"
    ]
    skipped = [m for m in result.matches if (m.internalization_decision or "").strip() == "无需内化"]
    lines = []
    if rows:
        lines = [
            "| 外规条款 | 外规要求（原文） | 内化判定 | 缺失原因 |",
            "|---|---|---|---|",
        ]
        for m in rows:
            lines.append(
                f"| {m.external_clause_id} | {m.external_source_text[:50]}… | "
                f"{_intl(m)} | {m.analysis[:60]} |"
            )
    else:
        lines.append("本部分无缺失的需内化条款。")
    if skipped:
        lines.append("")
        lines.append("以下条款判定**无需内化**(宣示性/程序性等),不构成制度缺口:")
        lines.append("、".join(m.external_clause_id for m in skipped))
    return "\n".join(lines)

def _template_report(result: CompareResult) -> str:
    """确定性回退报告:统计表 + 三节明细,证据原文直接引用。"""
    n = len(result.matches)
    skip = result.no_internalization_count
    scope = (
        f"分析范围：共 {n} 条外规条款（其中 {skip} 条判定无需内化），需内化处理 "
        f"{result.internalizable_count} 条；涉及 {len(result.external_documents)} 个外规制度、"
        f"{len(result.internal_documents)} 个内规制度。"
    )
    top_risks = [
        m for m in result.matches
        if m.status == CoverageStatus.NOT_COVERED
        and (m.internalization_decision or "").strip() != "无需内化"
    ][:3]
    risk_lines = "\n".join(
        f"- **{m.external_clause_id}**（{m.external_document_name}）：{m.analysis[:60]}"
        for m in top_risks
    ) or "- 无需补足的需内化条款。"

    return f"""# 内外规合规差距深度分析报告

## 摘要

- **{scope}**
- **关键风险点**：
{risk_lines}

## 覆盖情况总览

{_stats_table(result)}

## 内化判定总览

{_internalization_overview(result)}

## 三、完全承接的条款

{_covered_section(result)}

## 四、部分承接需补足的条款

{_partial_section(result)}

## 五、缺失的条款

{_missing_section(result)}

---
*本报告由模板自动生成(LLM 报告不可用时的确定性回退)。证据原文直接引用自比对结果。*
"""

# 对外入口

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
    llm: Any = None,
    *,
    output_dir: str | Path = "",
) -> dict[str, Any]:
    """生成差距分析报告(LLM 优先,模板回退)。

    Args:
        result: 比对结果。
        llm: ``LLM`` facade;None 或调用失败时用模板回退。
        output_dir: 非空则把报告写入该目录。

    Returns:
        {"report": str, "report_path": str, "mode": "llm" | "template"}
    """
    report = ""
    mode = "template"

    if llm is not None:
        try:
            skill = render("gap_report", datetime=date.today().isoformat())
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