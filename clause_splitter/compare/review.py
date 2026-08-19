"""HTML 复核展示页 —— 把每条外规条款的 top-k 内规召回呈现给作业人员.

比对跑完后,作业人员需要**看到**每条外规条款召回了哪些内规条款,才能判断
召回是否相关、结论是否可信。``render_review_html`` 把 ``CompareResult`` 渲染成
一个**自包含** HTML(内联 CSS/JS、emoji favicon、零外部资源,可直接双击打开):

- 顶部:外规制度、内规库、生成时间、覆盖统计 chips + 统计表(缺失只统计需内化
  条款,无需内化单列)。
- 控件:状态筛选 tab、关键词搜索、全部展开 / 收起。
- **无需内化条款集中折叠展示**(默认收起):宣示性/程序性等不纳入覆盖比对,
  不再按顺序混入卡片流。
- 每条**需要内化处理**的外规条款一张可折叠卡片:卡片头 = 条款号 + 覆盖状态徽标
  + 内化判定徽标(需要内化/无需内化/待判定,点击整卡折叠/展开);卡片体内依次为
  外规原文(折叠)、判定 analysis、部分覆盖的差异说明(内外规不一致处)、整改建议、
  检索召回 top-k。内化判定的详细依据不展示在页面,只在差距报告中体现。
- 「检索召回 top-k」按**来源制度分组**,组内用**表格**展示(列:条款号 | 关键词
  高亮 snippet | 后端 | 得分),采用行优先、得分降序,按全文去重;被 agent 采用
  的行标「✓」并浅绿高亮。被采用候选的全文即承接内规原文,不再另开
  「承接内规原文」块,避免重复。
- 内化判定只两值:需要内化 / 无需内化(「预防性内化」归并到「需要内化」)。
- 一次比对一个外规 → 一个 HTML,写在 ``{output_dir}/内规检索复核.html``。

所有用户文本先 ``html.escape`` 再进入模板,片段里的查询关键词用
``retrieve.highlight`` 包 ``<mark>``(已转义)。
"""

from __future__ import annotations

import asyncio
import html
from datetime import datetime
from pathlib import Path

from clause_splitter.config import (
    COMPARE_CONCURRENCY,
    REVIEW_FILTER_CANDIDATE_MAX_CHARS,
    REVIEW_FILTER_EXTERNAL_MAX_CHARS,
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

_STYLE = """
:root{--full:#2e7d32;--part:#f9a825;--miss:#c62828;--ink:#1a2332;--muted:#5b6b80;--bg:#f4f6fa;--card:#fff;--line:#e3e8f0;--mark-bg:#fff176}
*{box-sizing:border-box}
body{margin:0;font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--ink);background:var(--bg)}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 60px}
header h1{margin:0 0 4px;font-size:24px;display:flex;align-items:center;gap:8px}
.meta{color:var(--muted);font-size:13px;margin-bottom:14px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.chip{border:1px solid var(--line);background:#fff;border-radius:999px;padding:5px 14px;cursor:pointer;font-size:13px;color:var(--ink)}
.chip.active{box-shadow:inset 0 0 0 2px currentColor;font-weight:600}
.chip-full{color:var(--full)}.chip-part{color:var(--part)}.chip-miss{color:var(--miss)}
.controls{display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
#search{flex:1;min-width:220px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-size:14px}
.controls button{padding:8px 14px;border:1px solid var(--line);border-radius:8px;background:#fff;cursor:pointer;font-size:13px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat b{font-size:22px;display:block}
.stat span{color:var(--muted);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 20px 18px;margin-bottom:16px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card-head{display:flex;align-items:center;gap:10px;padding:4px 0;margin-bottom:8px;flex-wrap:wrap;cursor:pointer;user-select:none}
.card-head:hover .clause-id{text-decoration:underline}
.card-head .toggle-arrow{margin-left:auto;color:var(--muted);font-size:13px;transition:transform .12s}
.card-head.collapsed .toggle-arrow{transform:rotate(-90deg)}
.card-head.collapsed + .card-body{display:none}
.clause-id{font-size:16px;font-weight:700}
.badge{border-radius:6px;padding:2px 10px;font-size:12px;font-weight:600;color:#fff}
.badge-full{background:var(--full)}.badge-part{background:var(--part)}.badge-miss{background:var(--miss)}
.intl-badge{border-radius:6px;padding:2px 10px;font-size:12px;font-weight:600;border:1px solid transparent}
.intl-must{background:#fff0f0;color:#b71c1c;border-color:#f3c2c2}
.intl-none{background:#e8f5e9;color:#2e7d32;border-color:#c8e6c9}
.intl-pending{background:#eceff1;color:#78909c;border-color:#cfd8dc}
details{background:#fafbfd;border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:8px 0}
summary{cursor:pointer;font-weight:600;font-size:13px;color:var(--muted)}
pre{white-space:pre-wrap;word-break:break-word;font-family:inherit;font-size:13px;margin:8px 0 4px}
.analysis{background:#f8f9fb;border-left:3px solid #90a4ae;padding:8px 12px;border-radius:4px;margin:8px 0}
.analysis b{display:inline}
.match{margin:8px 0;color:var(--full)}
.diff{background:#fff8f0;border-left:3px solid #ef6c00;padding:8px 12px;border-radius:4px;margin:8px 0}
.diff-head{font-weight:600;font-size:13px;color:#b7791f;margin-bottom:2px}
.recall{margin-top:12px;border-top:1px dashed var(--line);padding-top:10px}
.recall h4{margin:0 0 8px;font-size:13px;color:var(--muted);font-weight:600}
.doc-group{border:1px solid var(--line);border-radius:8px;margin-bottom:10px;overflow:hidden}
.doc-group-head{background:#f4f6fb;padding:8px 12px;font-weight:600;font-size:13px;color:#37474f;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
.doc-group-count{color:var(--muted);font-weight:500;font-size:12px;margin-left:auto}
.cand-table{width:100%;border-collapse:collapse;font-size:13px}
.cand-table td{padding:9px 12px;border-top:1px solid var(--line);vertical-align:top}
.cand-row:first-child td{border-top:none}
.cand-row:hover td{background:#f8fafc}
.cand-row.adopted td{background:#f0faf3}
.cand-row.adopted:hover td{background:#e6f5eb}
.col-no{white-space:nowrap;font-weight:600;width:1%;color:var(--ink)}
.col-text{color:#334155}
.col-backend{white-space:nowrap;width:1%}
.col-score{white-space:nowrap;text-align:right;width:1%;color:var(--muted);font-variant-numeric:tabular-nums}
.adopted-mark{color:var(--full);font-weight:700}
.backend-badge{background:#eceff1;color:#455a64;border-radius:6px;padding:1px 8px;font-size:11px;white-space:nowrap}
mark{background:var(--mark-bg);padding:0 1px;border-radius:2px;font-weight:600}
.empty{color:var(--muted);font-style:italic;padding:6px 0}
.no-intl{border:1px solid var(--line);border-radius:10px;margin-bottom:16px;padding:6px 14px;background:#fff}
.no-intl summary{padding:2px 0}
.no-intl .hint{color:var(--muted);font-weight:400;margin-left:10px;font-size:12px}
.no-intl-rows{padding-top:2px}
.no-intl-row{display:flex;align-items:baseline;gap:10px;padding:8px 2px;border-top:1px dashed var(--line);font-size:13px}
.no-intl-row:first-child{border-top:none}
.no-intl-row .row-no{font-weight:700;white-space:nowrap}
.no-intl-row .row-text{flex:1;color:#334155;overflow-wrap:anywhere}
.no-intl-row .row-reason{color:var(--muted);font-size:12px;white-space:nowrap;max-width:45%;overflow:hidden;text-overflow:ellipsis}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:30px}
@media(max-width:720px){.stats{grid-template-columns:repeat(2,1fr)}.no-intl-row{flex-wrap:wrap}.no-intl-row .row-reason{max-width:100%;white-space:normal}}
"""

_JS = """
function setFilter(status){
  document.querySelectorAll('.chip').forEach(c=>c.classList.toggle('active', c.dataset.filter===status));
  applyFilters();
}
function applyFilters(){
  const status=document.querySelector('.chip.active').dataset.filter;
  const q=document.getElementById('search').value.trim().toLowerCase();
  document.querySelectorAll('.card').forEach(card=>{
    const okStatus = status==='all' || card.dataset.status===status;
    const okQuery = !q || card.textContent.toLowerCase().includes(q);
    card.style.display = (okStatus && okQuery) ? '' : 'none';
  });
  document.querySelectorAll('.no-intl-row').forEach(row=>{
    row.style.display = (!q || row.textContent.toLowerCase().includes(q)) ? '' : 'none';
  });
}
function setCardCollapsed(head, collapsed){
  head.classList.toggle('collapsed', collapsed);
  const arrow = head.querySelector('.toggle-arrow');
  if (arrow) arrow.textContent = collapsed ? '▸' : '▾';
}
function toggleAll(open){
  document.querySelectorAll('.card details').forEach(d=>d.open=open);
  document.querySelectorAll('.card-head').forEach(h=>setCardCollapsed(h, !open));
}
document.getElementById('search').addEventListener('input', applyFilters);
document.querySelectorAll('.chip').forEach(c=>c.addEventListener('click',()=>setFilter(c.dataset.filter)));
document.getElementById('expand-all').addEventListener('click',()=>toggleAll(true));
document.getElementById('collapse-all').addEventListener('click',()=>toggleAll(false));
document.querySelectorAll('.card-head').forEach(head=>{
  head.addEventListener('click', ()=>setCardCollapsed(head, !head.classList.contains('collapsed')));
  head.addEventListener('keydown', e=>{
    if (e.key==='Enter'||e.key===' ') { e.preventDefault(); setCardCollapsed(head, !head.classList.contains('collapsed')); }
  });
});
"""

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

    return [
        c for i, c in enumerate(cands)
        if (i + 1) in kept or c.adopted  # adopted(= grounding 采用)无条件保留
    ]


async def filter_relevant_candidates(
    result: CompareResult,
    llm,
    *,
    concurrency: int = COMPARE_CONCURRENCY,
) -> int:
    """LLM 过滤复核页候选:剔除与外规条款完全不相关的 top-k 候选(原地)。

    只处理候选非空的 match;``adopted`` 候选(= grounding 采用的条款)无条件保留;
    单条失败 → 该条保留全部(fail-open)。返回剔除的候选总数。

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
        "复核页相关性过滤完成:%d/%d 条外规条款参与判定,剔除 %d 条不相关候选",
        len(targets), len(result.matches), dropped,
    )
    return dropped


_INTL_BADGE = {
    "需要内化": ("intl-must", "需要内化"),
    "无需内化": ("intl-none", "无需内化"),
}


def _internalization_badge(m: MatchResult) -> str:
    """内化判定徽标(二值):需要内化(红)/ 无需内化(绿)/ 待判定(灰,空)。

    「预防性内化」经 normalize 归并到「需要内化」。
    """
    decision = normalize_internalization_decision((m.internalization_decision or "").strip())
    if not decision:
        return '<span class="intl-badge intl-pending">待判定</span>'
    cls, label = _INTL_BADGE.get(decision, ("intl-pending", html.escape(decision)))
    return f'<span class="intl-badge {cls}">{label}</span>'


def _is_no_internalization(m: MatchResult) -> bool:
    """是否判定「无需内化」(不纳入覆盖比对,复核页集中折叠展示)。"""
    return normalize_internalization_decision((m.internalization_decision or "").strip()) == "无需内化"


def _difference_html(m: MatchResult) -> str:
    """部分覆盖的差异说明(内外规不一致处);非部分覆盖或为空时不渲染。"""
    if m.status != CoverageStatus.PARTIALLY_COVERED or not (m.difference_analysis or "").strip():
        return ""
    return (
        '<div class="diff"><div class="diff-head">🔍 差异说明(内外规不一致处)</div>'
        f"<pre>{html.escape(m.difference_analysis)}</pre></div>"
    )


def _cand_html(m: MatchResult) -> str:
    """召回候选:按来源制度分组,组内用表格展示(条款号 | 高亮 snippet | 后端 | 得分)。

    组内采用行优先、得分降序,按全文去重——完全相同全文的重复候选被剔除,
    避免页面堆叠同一段文字;被采用的候选行打「✓」并浅绿高亮。
    """
    if not m.retrieved_candidates:
        return '<div class="empty">本条款没有可展示的检索召回(检索不可用,或全部召回经判定与本条款不相关)。</div>'
    groups: dict[str, list] = {}
    seen: set[str] = set()
    for cand in sorted(m.retrieved_candidates, key=lambda c: (not c.adopted, -(c.score or 0.0))):
        key = cand.full_text or cand.chunk_id
        if key in seen:
            continue
        seen.add(key)
        groups.setdefault(cand.doc_name or "(未命名)", []).append(cand)
    return "".join(_cand_table(doc, cands) for doc, cands in groups.items())


def _cand_table(doc: str, cands: list[RetrievedCandidate]) -> str:
    """一个来源制度的候选表格:表头(制度名 + 条数)+ 行。"""
    rows = "".join(_cand_row(c) for c in cands)
    return (
        '<div class="doc-group">'
        f'<div class="doc-group-head">📄 {html.escape(doc)}'
        f'<span class="doc-group-count">{len(cands)} 条</span></div>'
        f'<table class="cand-table"><tbody>{rows}</tbody></table>'
        "</div>"
    )


def _cand_row(cand: RetrievedCandidate) -> str:
    """表格一行:条款号(采用打勾)| 关键词高亮 snippet | 后端 | 得分。"""
    adopted = " adopted" if cand.adopted else ""
    mark = '<span class="adopted-mark">✓</span> ' if cand.adopted else ""
    score = f"{cand.score:.4f}" if cand.score is not None else ""
    return (
        f'<tr class="cand-row{adopted}">'
        f'<td class="col-no">{mark}{html.escape(cand.clause_label or cand.chunk_id)}</td>'
        f'<td class="col-text">{highlight(cand.snippet, cand.query)}</td>'
        f'<td class="col-backend"><span class="backend-badge">{html.escape(cand.backend)}</span></td>'
        f'<td class="col-score">{score}</td>'
        "</tr>"
    )


def _matched_refs(m: MatchResult) -> list[str]:
    """所有被采纳的内规条款(人类可读 ``制度名 第X条``)。

    优先用已标记 adopted 的召回候选(一条 episode 可能承接多条内规);
    候选缺失时回退到头部字段(仅第一条承接)。
    """
    refs = []
    for c in m.retrieved_candidates or []:
        if c.adopted:
            ref = f"{c.doc_name} {c.clause_label}".strip() or c.chunk_id
            if ref not in refs:
                refs.append(ref)
    if not refs and m.matched_internal_id:
        refs.append(f"{m.matched_internal_document_name or ''} {m.matched_internal_clause_number}".strip())
    return refs


def _no_intl_html(matches: list[MatchResult]) -> str:
    """「无需内化」条款集中展示(默认折叠),不再按顺序混入卡片流。

    每行:覆盖状态徽标 + 条款号 + 原文摘要 + 内化依据。不参与覆盖统计/整改。
    """
    if not matches:
        return ""
    rows = []
    for m in matches:
        cls, status_text = _BADGE[m.status]
        clause_id = html.escape(m.external_article_id or m.external_clause_id)
        src = m.external_source_text or ""
        snippet = html.escape(src[:90]) + ("…" if len(src) > 90 else "")
        reason = html.escape((m.internalization_reason or "").strip())
        rows.append(
            '<div class="no-intl-row">'
            f'<span class="badge {cls}">{status_text}</span>'
            f'<span class="row-no">{clause_id}</span>'
            f'<span class="row-text">{snippet}</span>'
            f'<span class="row-reason">{reason}</span>'
            "</div>"
        )
    return (
        '<details class="no-intl">'
        f"<summary>⚪ 无需内化条款 ({len(matches)} 条)"
        '<span class="hint">宣示性/程序性等,不纳入覆盖比对</span></summary>'
        f'<div class="no-intl-rows">{"".join(rows)}</div>'
        "</details>"
    )


def _card_html(m: MatchResult) -> str:
    badge_cls, status_text = _BADGE[m.status]
    clause_id = html.escape(m.external_article_id or m.external_clause_id)
    matched = ""
    refs = _matched_refs(m)
    if refs:
        matched = (
            '<div class="match">✓ 采用内规: '
            + ", ".join(f"<b>{html.escape(r)}</b>" for r in refs)
            + "</div>"
        )
    remedy = ""
    if m.remedy_suggestion:
        remedy = (
            "<details><summary>整改建议</summary>"
            f"<pre>{html.escape(m.remedy_suggestion)}</pre></details>"
        )
    return (
        f'<section class="card" data-status="{m.status.value}">'
        '<header class="card-head" role="button" tabindex="0" aria-expanded="true">'
        f'<span class="clause-id">{clause_id}</span>'
        f'<span class="badge {badge_cls}">{status_text}</span>'
        f"{_internalization_badge(m)}"
        '<span class="toggle-arrow">▾</span>'
        "</header>"
        '<div class="card-body">'
        "<details><summary>外规原文</summary>"
        f"<pre>{html.escape(m.external_source_text)}</pre></details>"
        f'<div class="analysis"><b>判定分析:</b> {html.escape(m.analysis)}</div>'
        f"{_difference_html(m)}"
        f"{matched}{remedy}"
        '<div class="recall"><h4>🔍 检索召回 top-k</h4>'
        f"{_cand_html(m)}"
        "</div>"
        "</div>"
        "</section>"
    )


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
    stats = {
        "完全覆盖": result.fully_covered_count,
        "部分覆盖": result.partially_covered_count,
        "缺失": result.internalizable_missing_count,
        "无需内化": result.no_internalization_count,
    }
    stats_cells = "".join(
        f'<div class="stat"><b>{n}</b><span>{k}</span></div>' for k, n in stats.items()
    )
    chips = "".join(
        f'<button class="chip chip-{cls}" data-filter="{f}"{" active" if f == "all" else ""}>'
        f"{label} ({count})</button>"
        for cls, f, label, count in (
            ("all", "all", "全部", len(card_matches)),
            ("full", CoverageStatus.FULLY_COVERED.value, "完全覆盖", result.fully_covered_count),
            ("part", CoverageStatus.PARTIALLY_COVERED.value, "部分覆盖", result.partially_covered_count),
            ("miss", CoverageStatus.NOT_COVERED.value, "缺失", result.internalizable_missing_count),
        )
    )
    if card_matches:
        cards = "".join(_card_html(m) for m in card_matches)
    else:
        cards = '<p class="empty">没有需要内化处理的条款(全部判定无需内化,或无比对结果)。</p>'
    no_intl_html = _no_intl_html(no_intl_matches)

    doc = (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n"
        '<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>内规检索复核 · {html.escape(ext_docs)}</title>\n"
        '<link rel="icon" href="data:image/svg+xml,'
        '<svg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 100 100\'>'
        '<text y=\'.9em\' font-size=\'90\'>⚖️</text></svg>\">\n'
        f"<style>{_STYLE}</style>\n</head>\n<body>\n"
        '<div class="wrap">\n'
        "<header>\n<h1>⚖️ 内规检索复核</h1>\n"
        f'<div class="meta">外规: <b>{html.escape(ext_docs)}</b>'
        f"{f' · 内规库: <b>{html.escape(store_name)}</b>' if store_name else ''}"
        f" · 生成时间: {datetime.now():%Y-%m-%d %H:%M}"
        f" · 覆盖率: {result.coverage_rate:.1%} (需内化处理 {result.internalizable_count} 条"
        f"{f', {result.no_internalization_count} 条无需内化' if result.no_internalization_count else ''})</div>\n"
        f'<div class="stats">{stats_cells}</div>\n'
        f'<div class="chips">{chips}</div>\n'
        '<div class="controls">'
        '<input id="search" type="search" placeholder="按关键词筛选条款 / 召回片段 / 无需内化列表…">'
        '<button id="expand-all">全部展开</button>'
        '<button id="collapse-all">全部收起</button>'
        "</div>\n"
        "</header>\n"
        f"<main>\n{no_intl_html}\n{cards}\n</main>\n"
        "<footer>「✓」= 该内规条款被比对 episode 采纳为承接条款;其余为批量预召回候选,供人工复核相关性。"
        "覆盖/缺失统计只算需内化处理条款,无需内化条款不构成制度缺口。</footer>\n"
        "</div>\n"
        f"<script>{_JS}</script>\n</body>\n</html>"
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
