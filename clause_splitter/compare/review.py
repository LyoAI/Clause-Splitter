"""HTML 复核展示页 —— 把每条外规条款的 top-k 内规召回呈现给作业人员.

比对跑完后,作业人员需要**看到**每条外规条款召回了哪些内规条款,才能判断
召回是否相关、结论是否可信。``render_review_html`` 把 ``CompareResult`` 渲染成
一个**自包含** HTML(内联 CSS/JS、emoji favicon、零外部资源,可直接双击打开):

- 顶部:外规制度、内规库、生成时间、覆盖统计 chips + 统计表(缺失只统计需内化
  条款,无需内化单列)。
- 控件:状态筛选 tab、关键词搜索、📊 导出 Excel、📝 导出批注、📥 导入批注
  (无全局全部展开/收起);检索召回区自带「展开更多」按钮,只展开当前召回区
  折叠的候选行。
- **无需内化条款集中折叠展示**(默认收起):宣示性/程序性等不纳入覆盖比对,
  不再按顺序混入卡片流。
- 每条**需要内化处理**的外规条款一张可折叠卡片(纵向单元):卡片头 = 条款号 +
  覆盖状态徽标 + 内化判定徽标(需要内化/无需内化/待判定,点击整卡折叠/展开)。
  卡片体内**外规原文保持原位**(顶部折叠),其下是**四栏并排**(桌面 4 栏,窄屏
  两两排布,手机单列堆叠,栏头简短):① 核心监管要求(内化判定依据为栏内
  虚线分隔的独立子块,给出为什么内化的原因) ② 差异说明(判定分析 + 内外规
  不一致处合并,栏内两小节,解决两栏各自单薄且内容重合的问题)
  ③ 整改建议 ④ 采用内规&检索召回(被采用条款摘要 + snippet + 全部检索召回
  top-k 表格)。四栏结构恒定,空栏以占位文案表达「已检查、无内容」。
  卡片内容区与第四栏栏体都是**固定高度窗口**(模板 CSS 变量 ``--card-window``
  / ``--panel-window``),内容超出时窗口内滚动(右侧细滑条),保证卡片高度一致、
  页面整齐。点击召回候选行或采用 snippet **弹窗**展示完整条款文本(遮罩淡入 +
  面板上浮动效)。
  判定分析/差异说明/核心监管要求/内化判定依据/整改建议按分点换行展示
  (旧数据无换行时按编号标记拆行)。
- **检索召回 top-k 收进第四栏**:含**被采用**与**未采用**的全部候选(被采用行
  标「✓」浅绿高亮)。按**来源制度分组**,组内用**表格**展示(列:条款号 |
  关键词高亮 snippet | 得分),采用行优先、得分降序、按全文去重;每个
  来源制度默认展示前 ``_RECALL_ROWS_SHOWN_PER_GROUP`` 条,**超出的行折叠**,
  由「展开更多 (N 条)」按钮展开;栏体在固定高度窗口内滚动。被采用候选的全文
  即承接内规原文,不再另开「承接内规原文」块,避免重复。
- 内化判定只两值:需要内化 / 无需内化(「预防性内化」归并到「需要内化」)。
- **批注与编辑(选中文字触发)**:用鼠标选中卡片内 核心监管要求/判定分析/
  差异说明/外规原文 的文字 → 浮出工具条「✍️ 编辑 / 💬 批注」。「编辑」弹窗
  展示引用的选中文字 + 修改输入,保存后该处文字替换、整行黄底标记;「批注」
  弹窗展示引用 + 署名(默认匿名)+ 批注内容,批注以黄色便签贴在卡片底部;
  引用处在文中以黄条点线标记,点击标记**原地弹出该处批注内容气泡**(可查看/
  删除,再点同一标记或点别处 / Esc / 滚动收起)。工具条选中即同步弹出,
  编辑/批注弹窗动效 10ms 近乎即时,批注保存后自动滚动定位到新便签。
  📝 导出批注产出 JSON 存档(类型/位置/条款号/字段/引用原文/修改文/署名/时间,
  修改记录附整单元最终全文),📥 导入批注按稳定 ``data-ann-id``(卡片序号:
  字段:行号)对位恢复——页面原文一致才回填,数据变更后错位项跳过;修改与
  批注同时自动暂存 localStorage,页面重新生成后不自动恢复。
- 进入页面弹**使用说明**弹窗(宫格功能卡,「⏳ 5 秒后自动关闭」倒计时 5→1,
  「我知道了」绿色框按钮可立即关闭)。
- 一次比对一个外规 → 一个 HTML,写在 ``{output_dir}/内规检索复核.html``。

页面模板在 ``templates/review.html``(Jinja2,``autoescape=True``):CSS/JS 内联在
模板里,输出仍是零外部资源的自包含 HTML。Python 侧只做数据准备(把行文本、
候选分组等整理成 dict),不再拼 HTML 字符串;唯一预渲染的 HTML 片段是
``retrieve.highlight`` 包出的关键词高亮 snippet(在模板里 ``|safe`` 放行),
其余用户文本全由 Jinja 自动转义。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from clause_splitter.config import (
    COMPARE_CONCURRENCY,
    REVIEW_FILTER_CANDIDATE_MAX_CHARS,
    REVIEW_FILTER_EXTERNAL_MAX_CHARS,
    REVIEW_MIN_CANDIDATES,
)
from clause_splitter.models.compare import (
    CompareResult,
    CoverageStatus,
    MatchResult,
    RetrievedCandidate,
    normalize_internalization_decision,
)
from clause_splitter.utils import get_logger
from clause_splitter.utils.file.json_utils import parse_json_from_text
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


# ═══════════════════════════════════════════════════════════════════════
# 候选相关性过滤 —— LLM 判断 top-k 候选是否与外规条款「有一点关联」
# ═══════════════════════════════════════════════════════════════════════

_RELEVANCE_SYSTEM = (
    "你是一名合规比对审核助手,负责对外规条款的检索召回候选做「相关/不相关」的严格二分类。\n"
    "你会看到一条外规条款,以及若干候选内规条款(每条含:所属制度文件标题、条款号、条款内容摘录)。\n"
    "请依据「制度文件标题 + 条款内容」判断每个候选内规条款与此外规条款是否真正相关:\n"
    "- 判定为「相关」:两者属于同一监管主题/合规领域,且候选条款内容实质性承接、呼应或覆盖了该外规要求"
    "(例如外规要求数据分类分级,候选正是数据分类分级管理制度下直接相关的条款)。\n"
    "- 判定为「不相关」:仅因关键词相似被召回,但制度标题或条款内容与该外规不属同一合规领域、无实质承接关系"
    "(例如外规是数据安全,候选是财务报销、人事考勤);或条款内容只是泛泛提及、未实质性涉及外规要求。\n"
    "宁可漏判不可误留:对无法明确判定相关性的候选,一律判为不相关并剔除,避免页面展示无关信息。\n"
    '只输出 JSON,不要任何解释文字。格式:{"related": [候选编号...]},编号从 1 到候选总数;'
    '没有相关的就输出 {"related": []}。'
)


def _truncate(text: str, max_chars: int) -> str:
    """按字符截断,并标注省略。"""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _parse_related_indices(content: str, n: int) -> set[int] | None:
    """从 LLM 输出解析 1 起始候选编号集合;任何非法 → None(触发 fail-open)。

    注意:``parse_json_from_text`` 失败一律返回 ``{}``,所以 ``{}`` / 散文串 /
    裸数组都落到这里:related 键缺失 → None(保留全部),绝不静默全删。这是唯一
    可靠的兜底——PingAn provider 在 API 失败时返回**非空散文串**
    ("Error: Failed to get response from LLM."),content 并不是 None。
    """
    if not content or not content.strip():
        return None
    try:
        data = parse_json_from_text(content)
    except Exception:  # noqa: BLE001
        return None
    related = data.get("related")
    if not isinstance(related, list):
        return None
    indices: set[int] = set()
    for v in related:
        if not isinstance(v, int) or isinstance(v, bool) or v < 1 or v > n:
            return None  # 0 / 越界 / 非整数 → fail-open,宁多勿少
        indices.add(v)
    return indices


async def _judge_candidates(llm, match: MatchResult) -> list[RetrievedCandidate]:
    """一次 LLM 调用判定该条外规的所有候选;失败则保留全部(fail-open)。

    返回该条应展示的候选子集(原地改写由 ``filter_relevant_candidates`` 负责)。
    """
    cands = match.retrieved_candidates
    lines = [
        f"外规条款:\n{_truncate(match.external_source_text, REVIEW_FILTER_EXTERNAL_MAX_CHARS)}\n",
        "候选内规条款:",
    ]
    for i, c in enumerate(cands, 1):
        lines.append(
            f"{i}. [{c.doc_name}] {c.clause_label or c.chunk_id}\n"
            f"{_truncate(c.full_text, REVIEW_FILTER_CANDIDATE_MAX_CHARS)}"
        )
    user_prompt = "\n\n".join(lines) + "\n"

    try:
        resp = await llm.ainvoke([
            {"role": "system", "content": _RELEVANCE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ], temperature=0.0)
    except Exception as e:  # noqa: BLE001 — 任何失败都 fail-open
        logger.warning("候选相关性判定失败(保留全部): %s", e)
        return cands

    content = resp.content if hasattr(resp, "content") else str(resp)
    kept = _parse_related_indices(content, len(cands))
    if kept is None:
        logger.warning("候选相关性判定输出不可解析(保留全部): %r", (content or "")[:80])
        return cands

    relevant = [
        c for i, c in enumerate(cands)
        if (i + 1) in kept or c.adopted  # adopted(= grounding 采用)无条件保留
    ]
    # 最少展示 REVIEW_MIN_CANDIDATES 条:不足时按得分降序从全部候选补齐(不论相关性),
    # 避免严格过滤把召回清空、页面出现「没有可展示的检索召回」。
    return _min_display_candidates(relevant, cands)


def _min_display_candidates(
    kept: list[RetrievedCandidate],
    all_cands: list[RetrievedCandidate],
    minimum: int = REVIEW_MIN_CANDIDATES,
) -> list[RetrievedCandidate]:
    """候选不足 minimum 条时,按得分降序从全部候选补齐(保留的在前)。

    采用条款已在 kept 中(adopted 无条件保留);补齐只填得分最高的候选,确保
    复核页始终能看到至少 minimum 条召回,除非原本就一条候选都没有。
    """
    if len(kept) >= minimum:
        return kept
    kept_ids = {c.chunk_id for c in kept}
    topup = [
        c for c in sorted(all_cands, key=lambda c: -(c.score or 0.0))
        if c.chunk_id not in kept_ids
    ]
    return kept + topup[: minimum - len(kept)]


async def filter_relevant_candidates(
    result: CompareResult,
    llm,
    *,
    concurrency: int = COMPARE_CONCURRENCY,
) -> int:
    """LLM 过滤复核页候选:剔除与外规条款完全不相关的 top-k 候选(原地)。

    只处理候选非空的 match;``adopted`` 候选(= grounding 采用的条款)无条件保留;
    单条失败 → 该条保留全部(fail-open)。每条最终至少展示 ``REVIEW_MIN_CANDIDATES``
    条(不足时按得分补齐,不论相关性),故「净剔除」= 原始候选数 - 最终候选数。

    注意:调用方必须把它放在 ``compare_result.json`` 落盘**之后**——过滤会原地
    改写 ``match.retrieved_candidates``,若在落盘前调用会静默剥掉 json 里的候选。
    """
    if llm is None:
        return 0
    targets = [m for m in result.matches if m.retrieved_candidates]
    if not targets:
        return 0

    sem = asyncio.Semaphore(concurrency)
    dropped = 0

    async def _one(m: MatchResult) -> None:
        nonlocal dropped
        async with sem:
            kept = await _judge_candidates(llm, m)
            dropped += len(m.retrieved_candidates) - len(kept)
            m.retrieved_candidates = kept

    await asyncio.gather(*(_one(m) for m in targets))
    logger.info(
        "复核页相关性过滤完成:%d/%d 条外规条款参与判定,净剔除 %d 条候选(每条至少展示 %d 条)",
        len(targets), len(result.matches), dropped, REVIEW_MIN_CANDIDATES,
    )
    return dropped


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
        coverage_rate=f"{result.coverage_rate:.1%}",
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
