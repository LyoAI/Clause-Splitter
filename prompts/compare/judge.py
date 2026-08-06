"""Judge prompt — one external obligation vs its retrieved internal candidates.

The judge does NOT search. It receives exactly the candidates BM25 retrieved
and must pick one (by row number) or declare 缺失. Emitting a row number
instead of an obligation id structurally prevents id fabrication: there is no
way to cite a clause that was never retrieved.
"""

from __future__ import annotations

from typing import Any

JUDGE_SYSTEM_PROMPT = """# 法规条款承接判定器

你是一位资深法律合规审计专家。系统已经通过 BM25 检索，从内规库中为你召回了与当前外部义务**最相关的候选条款**。
你的唯一任务：判断这条「外部义务」是否被候选表中的内规条款承接。

## 三条铁律（违反即为错误）
1. **只能从候选表中选择承接条款**。候选表里没有的，就是内规库中没有 —— 严禁编造、联想或"推断"候选表之外的内规条款。
2. 输出中的 `matched_candidate_index` 必须是**候选表的行号（正整数）**，不是内规义务ID、不是制度名、不是条款号。
3. **"缺失"不是失败，而是最有价值的发现**。找不到承接就如实判"缺失"，绝不为了凑一个匹配而强行挑选。

## 判定标准
- **完全覆盖**：候选表中某个内规条款，义务主体/动作/事项/规则方向与外规一致，且要求不弱于外规。
  特别注意：如果某个候选的原文与外规原文**相同或高度一致（仅措辞略有出入）→ 必须判"完全覆盖"**。
- **部分覆盖**：候选涉及同一事项，但遗漏了部分子要求、主体范围不一致、约束力度较弱，或需多个候选拼合才能满足。
- **缺失**：候选表中没有任何条款真正承接该外规义务。

## 输出格式（只输出 JSON，不要代码块标记、不要多余文字）

```json
{
  "external_clause_id": "外规义务序号（与输入一致）",
  "status": "完全覆盖 | 部分覆盖 | 缺失",
  "matched_candidate_index": "承接候选表的行号（覆盖/部分覆盖时为 1~N 的整数；缺失时为 null）",
  "analysis": "判定理由。必须基于候选表中的条款原文；完全覆盖/部分覆盖时引用该候选原文关键句作为依据；缺失时说明为什么候选都不承接。",
  "remedy_suggestion": "整改建议（缺失/部分覆盖时必填；完全覆盖可为空字符串）"
}
```
"""


def _table_cell(value: Any) -> str:
    """Sanitize a cell value for a markdown table row."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def build_judge_user_prompt(
    external_obl: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    """Format the judge user prompt for one external obligation.

    Args:
        external_obl: One external ClauseObligation dict.
        candidates: Retrieved candidates, each ``{docid, score, obligation}``
            (the shape returned by ``BM25Searcher.search``).

    Returns:
        User prompt string with the obligation and the candidate table.
    """
    lines = [
        "## 外部义务",
        f"- 序号: #{external_obl.get('index', '?')}",
        f"- 条款: {_table_cell(external_obl.get('clause_number', '') or '/')}",
        f"- 义务主体: {_table_cell(external_obl.get('subject', '') or '/')}",
        f"- 义务规则: {_table_cell(external_obl.get('obligation_rule', '') or '/')}",
        f"- 义务动作: {_table_cell(external_obl.get('action_verb', '') or '/')}",
        f"- 义务事项: {_table_cell(external_obl.get('action_target', '') or '/')}",
        f"- 引用原文: {_table_cell(external_obl.get('source_text', ''))}",
        "",
        f"## 内规候选（BM25 召回，共 {len(candidates)} 条）",
        "",
        "| 行号 | 内规义务ID | BM25分 | 制度 | 条款 | 候选原文 |",
        "|---|---|---|---|---|---|",
    ]

    for i, candidate in enumerate(candidates, start=1):
        obl = candidate.get("obligation", {})
        doc_name = obl.get("document_name", "")
        index = obl.get("index", "")
        clause = obl.get("clause_number", "")
        source = obl.get("source_text", "")
        lines.append(
            f"| {i} | {_table_cell(doc_name)}::{_table_cell(index)} "
            f"| {candidate.get('score', 0):.2f} "
            f"| {_table_cell(doc_name)} | {_table_cell(clause)} "
            f"| {_table_cell(source)} |"
        )

    lines.append("")
    lines.append("请基于上表判定，只输出 JSON。")
    return "\n".join(lines)
