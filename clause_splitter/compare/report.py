"""差距分析报告生成 —— skills/summary/SKILL.md + LLM,失败时模板回退.

报告是比对的最后产物。``generate_gap_report`` 先尝试让 LLM 按
``skills/summary/SKILL.md`` 撰写正式报告(语气专业、证据引用原文);
LLM 失败或未注入时,回退到确定性的 ``_template_report`` —— 纯模板拼装,
统计表 + 全部 MatchResult 逐行呈现,证据原文直接引用,绝不空谈。

两种路径都产出完整 Markdown,写入 ``{output_dir}/内外规合规差距分析报告.md``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clause_splitter.config import REPO_ROOT
from clause_splitter.models import CompareResult, CoverageStatus
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
# LLM 报告
# ═══════════════════════════════════════════════════════════════════════


def _build_external_clause_md(result: CompareResult) -> str:
    """外规条款原文(markdown),供 LLM 报告引用。"""
    lines = []
    for m in result.matches:
        lines.append(f"### {m.external_clause_id}")
        lines.append(f"出处: {m.external_document_name} {m.external_article_id or ''}".rstrip())
        lines.append(f"原文:\n\n{m.external_source_text}")
        lines.append("")
    return "\n".join(lines)


def _build_internal_clause_md(store: DocumentStore) -> str:
    """内规条款全文(markdown),供 LLM 报告建议新增/修订条款时参考。"""
    lines = []
    for chunk in store.all_chunks():
        meta = chunk.meta or {}
        lines.append(f"### {chunk.chunk_id}")
        section = meta.get("section", "")
        if section:
            lines.append(f"章节: {section}")
        lines.append(f"原文:\n\n{chunk.text}")
        lines.append("")
    return "\n".join(lines)


def _build_user_prompt(
    result: CompareResult,
    external_md: str,
    internal_md: str,
) -> str:
    """把比对结果 + 内外规原文填进 summary SKILL 的用户输入槽位。"""
    return (
        "{clause_match_results}\n\n"
        "### 外部规定条款\n\n"
        "{external_clause_md}\n\n"
        "### 内部规章制度\n\n"
        "{internal_clause_md}\n"
    ).format(
        clause_match_results=json.dumps(
            result.to_summary_dict(), ensure_ascii=False, indent=2
        ),
        external_clause_md=external_md,
        internal_clause_md=internal_md,
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
- **核心发现**：完全覆盖 {result.fully_covered_count} 条、部分覆盖 {result.partially_covered_count} 条、缺失 {result.not_covered_count} 条（覆盖率 {result.coverage_rate:.1%}）。
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
        store: 内规条款库(供 LLM 参考内规全文)。
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
            user_prompt = _build_user_prompt(
                result,
                _build_external_clause_md(result),
                _build_internal_clause_md(store),
            )
            resp = llm.invoke([
                {"role": "system", "content": skill},
                {"role": "user", "content": user_prompt},
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
