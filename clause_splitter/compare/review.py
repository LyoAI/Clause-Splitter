"""HTML 复核展示页 —— 把每条外规条款的 top-k 内规召回呈现给作业人员.

``render_review_html`` 把 ``CompareResult`` 渲染成自包含 HTML(内联 CSS/JS,
无外部资源),写到 ``{output_dir}/内规检索复核.html``。页面结构与交互都在
``templates/review.html``;Python 侧只准备数据(行文本、候选分组等),不拼 HTML。
唯一的预渲染片段是 ``retrieve.highlight`` 的关键词高亮 snippet(模板中 ``|safe``),
其余文本由 Jinja 自动转义。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from clause_splitter.models import (
    CompareResult,
    CoverageStatus,
    MatchResult,
    RetrievedCandidate,
    normalize_internalization_decision,
)
from clause_splitter.utils import get_logger
from .retrieve import highlight

logger = get_logger()

#: 页面模板(Jinja2,``autoescape=True``):CSS/JS 内联在模板里,输出仍是
#: 零外部资源的自包含 HTML,模板路径与模块同目录下的 ``templates/``。
_TEMPLATE = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=True,
).get_template("review.html")

_BADGE = {
    CoverageStatus.FULLY_COVERED: ("badge-full", "完全覆盖"),
    CoverageStatus.PARTIALLY_COVERED: ("badge-part", "部分覆盖"),
    CoverageStatus.NOT_COVERED: ("badge-miss", "缺失"),
}


def _truncate(text: str, max_chars: int) -> str:
    """按字符截断,并标注省略。"""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


_INTL_BADGE = {
    "需要内化": ("intl-must", "需要内化"),
    "无需内化": ("intl-none", "无需内化"),
}


def _intl_badge_data(m: MatchResult) -> tuple[str, str]:
    """内化判定徽标数据 ``(css_class, label)``(二值)。

    需要内化(红)/ 无需内化(绿)/ 待判定(灰,空);「预防性内化」经 normalize
    归并到「需要内化」。HTML 由模板渲染,label 交给 Jinja 自动转义。
    """
    decision = normalize_internalization_decision((m.internalization_decision or "").strip())
    if not decision:
        return "intl-pending", "待判定"
    cls, label = _INTL_BADGE.get(decision, ("intl-pending", decision))
    return cls, label


def _is_no_internalization(m: MatchResult) -> bool:
    """是否判定「无需内化」(不纳入覆盖比对,复核页集中折叠展示)。"""
    return normalize_internalization_decision((m.internalization_decision or "").strip()) == "无需内化"


#: 分点标记(编号开头),用于旧数据无换行时拆行:
#: 1）、2.3) / （1）(2) / 一、二、 / ①②(全角 ） U+FF09 也要收进字符类)
_NUM_MARK = re.compile(
    r"(?=[（(]\d+[）)])|(?=\d+[、.)）])|(?=[一二三四五六七八九十]+、)|(?=[①②③④⑤⑥⑦⑧⑨⑩])"
)


def _split_lines(text: str) -> list[str]:
    """把多行/多分点文本拆成行列表(模板逐行渲染成 ``<div class="line">``)。

    优先按换行分点;单行但含编号标记(旧数据)时按标记拆行——保证每个分点
    独占一行,不再揉在一起。返回**原始文本行**,转义交给 Jinja 自动转义。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    if len(lines) == 1:
        lines = [p.strip() for p in _NUM_MARK.split(raw) if p.strip()]
    return lines


#: 检索召回每个来源制度默认展示的行数,超出折叠。必须与模板 CSS 的
#: ``.recall-body.truncated .cand-table tbody tr:nth-of-type(n+3)`` 保持一致
#: (展示前 2 行,第 3 行起隐藏;行数 K 对应选择器 n+(K+1))。
_RECALL_ROWS_SHOWN_PER_GROUP = 2


def _group_candidates(cands: list[RetrievedCandidate]) -> tuple[list[dict], int]:
    """候选按来源制度分组(采用行优先、得分降序,按全文去重)。

    返回 ``(groups, more_count)``:``groups`` 是 ``[{"doc", "count", "rows"}]``
    的有序列表(rows 为 ``_cand_row_data`` dict);``more_count`` = 各组超过默认
    展示行数的合计,大于 0 时召回区渲染「展开更多 (N 条)」按钮。只做数据准备,
    HTML 渲染交给模板。
    """
    if not cands:
        return [], 0
    groups: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for cand in sorted(cands, key=lambda c: (not c.adopted, -(c.score or 0.0))):
        key = cand.full_text or cand.chunk_id
        if key in seen:
            continue
        seen.add(key)
        groups.setdefault(cand.doc_name or "(未命名)", []).append(_cand_row_data(cand))
    more_count = sum(
        max(0, len(rows) - _RECALL_ROWS_SHOWN_PER_GROUP) for rows in groups.values()
    )
    return [
        {"doc": doc, "count": len(rows), "rows": rows}
        for doc, rows in groups.items()
    ], more_count


def _cand_row_data(cand: RetrievedCandidate) -> dict:
    """表格一行的数据:条款号(采用打勾)| 高亮 snippet | 得分。

    不展示检索后端列(窄栏里把宽度让给条款文本;后端信息保留在导出 Excel 的
    召回行里)。``snippet_html`` 是唯一预渲染的 HTML(``highlight`` 包 ``<mark>``,
    已转义),模板里 ``|safe`` 放行;``full`` 是完整条款文本(渲染进 ``data-full``
    属性,点击该行弹窗展示),其余字段交 Jinja 自动转义。
    """
    # 补入的采用条款来自比对检索(episode),无批量召回得分 → 显示「—」。
    score = f"{cand.score:.4f}" if cand.score is not None else "—"
    return {
        "label": cand.clause_label or cand.chunk_id,
        "snippet_html": highlight(cand.snippet, cand.query),
        "score": score,
        "adopted": cand.adopted,
        "full": cand.full_text or cand.snippet or "",
    }


#: 第四栏「采用内规」每条被采用条款的 snippet 最大字符数(简要展示内容摘要)。
_MATCH_SNIPPET_MAX_CHARS = 80


def _matched_refs(m: MatchResult) -> list[dict]:
    """所有被采纳的内规条款 ``[{"ref", "snippet", "full"}]``(``制度名 第X条``)。

    优先用已标记 adopted 的召回候选(一条 episode 可能承接多条内规);
    候选缺失时回退到头部字段(仅第一条承接)。``snippet`` 是被采用条款全文的
    截断摘要(第四栏简要展示),``full`` 是完整文本(点击 snippet 弹窗展示),
    原始文本交 Jinja 自动转义。
    """
    refs: list[dict] = []
    seen: set[str] = set()
    for c in m.retrieved_candidates or []:
        if c.adopted:
            ref = f"{c.doc_name} {c.clause_label}".strip() or c.chunk_id
            if ref in seen:
                continue
            seen.add(ref)
            refs.append({
                "ref": ref,
                "snippet": _truncate(c.full_text or c.snippet or "", _MATCH_SNIPPET_MAX_CHARS),
                "full": c.full_text or c.snippet or "",
            })
    if not refs and m.matched_internal_id:
        refs.append({
            "ref": f"{m.matched_internal_document_name or ''} {m.matched_internal_clause_number}".strip(),
            "snippet": _truncate(m.matched_internal_source_text or "", _MATCH_SNIPPET_MAX_CHARS),
            "full": m.matched_internal_source_text or "",
        })
    return refs


def _no_intl_data(m: MatchResult) -> dict:
    """「无需内化」条款的一条数据(模板渲染上下布局,默认折叠)。

    上一行 = 覆盖状态徽标 + 条款号 + 外规原文(完整);下一行 = 判定依据
    (完整,不截断)。不参与覆盖统计/整改。只做数据准备,HTML 交给模板。
    """
    cls, status_text = _BADGE[m.status]
    return {
        "badge_cls": cls,
        "status_text": status_text,
        "clause_id": m.external_article_id or m.external_clause_id,
        "external_source_text": m.external_source_text or "",
        "reason": (m.internalization_reason or "").strip(),
    }


def _card_data(m: MatchResult) -> dict:
    """一张外规条款卡片的数据(模板渲染纵向单元 + 四栏并排)。

    四栏 = ① 核心监管要求(内化判定依据为栏内独立子块) ② 差异说明(判定分析 +
    内外规不一致处合并,栏内两小节) ③ 整改建议 ④ 采用内规&检索召回,每栏内部
    纵向;桌面 4 栏并排,窄屏(≤1100px)两两排布,手机单列堆叠。差异说明按状态
    给占位(完全覆盖=无差异,缺失=无内规可对比),四栏结构恒定。检索召回(含
    采用/未采用)收进第四栏,栏体固定高度窗口滚动。只做数据准备,HTML 渲染交给
    模板(``templates/review.html``)。
    """
    badge_cls, status_text = _BADGE[m.status]
    intl_cls, intl_label = _intl_badge_data(m)
    cands = m.retrieved_candidates or []
    # 全部候选按来源制度分组展示(采用行优先、得分降序、按全文去重);被采用
    # 的行会在召回表中标「✓」浅绿高亮,第四栏只留「采用内规」摘要。
    groups, more_count = _group_candidates(cands)
    if m.status == CoverageStatus.NOT_COVERED:
        diff_empty = "缺失，无内规可对比"
    elif m.status == CoverageStatus.PARTIALLY_COVERED:
        diff_empty = "—"
    else:
        diff_empty = "无内外规差异"
    return {
        "status": m.status.value,
        "clause_id": m.external_article_id or m.external_clause_id,
        "badge_cls": badge_cls,
        "status_text": status_text,
        "intl_cls": intl_cls,
        "intl_label": intl_label,
        "external_source_text": m.external_source_text or "",
        "core_lines": _split_lines(m.core_regulatory_requirements),
        "core_empty": "无具体合规义务（定义/目的/程序性质条款）",
        "intl_reason_lines": _split_lines(m.internalization_reason),
        "analysis_lines": _split_lines(m.analysis),
        "diff_lines": (
            _split_lines(m.difference_analysis)
            if m.status == CoverageStatus.PARTIALLY_COVERED else []
        ),
        "diff_empty": diff_empty,
        "remedy_lines": _split_lines(m.remedy_suggestion),
        "remedy_empty": "—",
        "refs": _matched_refs(m),
        "total_candidates": len(cands),
        "groups": groups,
        "more_count": more_count,
    }


def _export_row(m: MatchResult) -> dict:
    """导出 Excel 的一行(模板把全部行以 JSON 内嵌进页面,由「导出 Excel」按钮
    在前端生成 .xls,页面保持零外部依赖)。

    文本一律用原始值(不带高亮 ``<mark>``);检索召回与页面展示同一顺序
    (采用优先、得分降序、按全文去重),被采用候选带「✓」前缀;差异说明仅
    部分覆盖导出。含核心监管要求 / 内化判定依据 / 整改建议(与页面四栏对齐)。
    """
    seen: set[str] = set()
    recall_lines: list[str] = []
    for c in sorted(m.retrieved_candidates or [], key=lambda c: (not c.adopted, -(c.score or 0.0))):
        key = c.full_text or c.chunk_id
        if key in seen:
            continue
        seen.add(key)
        mark = "✓ " if c.adopted else ""
        score = f"{c.score:.4f}" if c.score is not None else "—"
        ref = f"{c.doc_name} {c.clause_label or c.chunk_id}".strip()
        recall_lines.append(f"{mark}{ref}（{c.backend or '—'} {score}）\n{c.full_text or c.snippet or ''}")
    refs = _matched_refs(m)
    adopted = "\n\n".join(f"【{r['ref']}】\n{r['snippet']}" for r in refs) if refs else "—"
    diff = m.difference_analysis if m.status == CoverageStatus.PARTIALLY_COVERED else ""
    return {
        "clause": m.external_article_id or m.external_clause_id,
        "status": m.status.value,
        "external": m.external_source_text or "",
        "core": m.core_regulatory_requirements or "",
        "intl_reason": m.internalization_reason or "",
        "analysis": m.analysis or "",
        "diff": diff or "",
        "remedy": m.remedy_suggestion or "",
        "adopted": adopted,
        "recall": "\n\n".join(recall_lines),
    }


def render_review_html(
    result: CompareResult,
    output_path: str | Path,
    *,
    store_name: str = "",
) -> Path:
    """Render ``result`` to a self-contained HTML review page at ``output_path``.

    Returns ``output_path`` (a ``Path``). Every candidate snippet is
    keyword-highlighted; all user text is HTML-escaped.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ext_docs = "、".join(result.external_documents) or "(未命名)"
    no_intl_matches = [m for m in result.matches if _is_no_internalization(m)]
    card_matches = [m for m in result.matches if not _is_no_internalization(m)]
    # 缺失只统计需内化处理的条款(无需内化单列,不当作缺口)。
    stats = [
        {"cls": "s-full", "count": result.fully_covered_count, "label": "完全覆盖"},
        {"cls": "s-part", "count": result.partially_covered_count, "label": "部分覆盖"},
        {"cls": "s-miss", "count": result.internalizable_missing_count, "label": "缺失"},
        {"cls": "s-none", "count": result.no_internalization_count, "label": "无需内化"},
    ]
    chips = [
        {"cls": "all", "filter": "all", "label": "全部", "count": len(card_matches)},
        {"cls": "full", "filter": CoverageStatus.FULLY_COVERED.value, "label": "完全覆盖",
         "count": result.fully_covered_count},
        {"cls": "part", "filter": CoverageStatus.PARTIALLY_COVERED.value, "label": "部分覆盖",
         "count": result.partially_covered_count},
        {"cls": "miss", "filter": CoverageStatus.NOT_COVERED.value, "label": "缺失",
         "count": result.internalizable_missing_count},
    ]

    generated_at = f"{datetime.now():%Y-%m-%d %H:%M}"
    doc = _TEMPLATE.render(
        ext_docs=ext_docs,
        store_name=store_name,
        generated_at=generated_at,
        coverage_rate=(
            f"{result.fully_covered_count + result.partially_covered_count}"
            f"/{result.internalizable_count}"
        ),
        internalizable_count=result.internalizable_count,
        no_internalization_count=result.no_internalization_count,
        stats=stats,
        chips=chips,
        no_intl=[_no_intl_data(m) for m in no_intl_matches],
        cards=[_card_data(m) for m in card_matches],
        # 导出 Excel / 批注存档的前端数据源(文件名剔除非法字符;generated_at
        # 供批注存档识别页面版本,页面重新生成后旧批注不自动恢复)。
        export_data={
            "name": f"内规检索复核-{re.sub(r'[\\/:*?\"<>|]', '_', ext_docs)[:40]}",
            "generated_at": generated_at,
            "rows": [_export_row(m) for m in card_matches],
        },
    )
    output_path.write_text(doc, encoding="utf-8")
    logger.info("内规检索复核页已生成: %s (%d 条款, %d 召回候选)",
                output_path, len(result.matches),
                sum(len(m.retrieved_candidates) for m in result.matches))
    return output_path


def review_page_metadata(result: CompareResult) -> dict:
    """Compact JSON metadata about the review page (for API responses)."""
    return {
        "external_documents": result.external_documents,
        "internal_documents": result.internal_documents,
        "total_external_obligations": result.total_external_obligations,
        "coverage_rate": f"{result.coverage_rate:.1%}",
        "stats": {
            "fully_covered": result.fully_covered_count,
            "partially_covered": result.partially_covered_count,
            "not_covered": result.not_covered_count,
        },
    }
