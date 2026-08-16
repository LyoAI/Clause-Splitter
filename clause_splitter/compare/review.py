"""HTML 复核展示页 —— 把每条外规条款的 top-k 内规召回呈现给作业人员.

比对跑完后,作业人员需要**看到**每条外规条款召回了哪些内规条款,才能判断
召回是否相关、结论是否可信。``render_review_html`` 把 ``CompareResult`` 渲染成
一个**自包含** HTML(内联 CSS/JS、emoji favicon、零外部资源,可直接双击打开):

- 顶部:外规制度、内规库、生成时间、覆盖统计 chips + 统计表。
- 控件:状态筛选 tab、关键词搜索、全部展开 / 收起。
- 每条外规条款一张卡片:条款号 + 状态徽标 + 外规原文(折叠)+ 判定 analysis /
  整改建议;其下「检索召回 top-k」逐条候选:制度名徽标 + 条款标签 + 检索后端徽标
  + 得分 + 命中片段(查询词 ``<mark>`` 高亮)+ 全文可展开;被 agent 采用的标「✓ 采用」。
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
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card-head{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.clause-id{font-size:16px;font-weight:700}
.badge{border-radius:6px;padding:2px 10px;font-size:12px;font-weight:600;color:#fff}
.badge-full{background:var(--full)}.badge-part{background:var(--part)}.badge-miss{background:var(--miss)}
details{background:#fafbfd;border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:8px 0}
summary{cursor:pointer;font-weight:600;font-size:13px;color:var(--muted)}
pre{white-space:pre-wrap;word-break:break-word;font-family:inherit;font-size:13px;margin:8px 0 4px}
.analysis{background:#f8f9fb;border-left:3px solid #90a4ae;padding:8px 12px;border-radius:4px;margin:8px 0}
.analysis b{display:inline}
.match{margin:8px 0;color:var(--full)}
.recall{margin-top:12px;border-top:1px dashed var(--line);padding-top:10px}
.recall h4{margin:0 0 8px;font-size:13px;color:var(--muted);font-weight:600}
.cand{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:8px;background:#fff}
.cand-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.doc-badge{background:#eef2ff;color:#3730a3;border-radius:6px;padding:1px 8px;font-size:12px;font-weight:600}
.label{color:var(--ink);font-weight:600;font-size:13px}
.backend-badge{background:#eceff1;color:#455a64;border-radius:6px;padding:1px 8px;font-size:11px}
.score{color:var(--muted);font-size:12px}
.adopted{background:#e6f4ea;color:#1e6b3a;border-radius:6px;padding:1px 8px;font-size:12px;font-weight:700}
.snippet{font-size:13px;color:#334155}
mark{background:var(--mark-bg);padding:0 1px;border-radius:2px;font-weight:600}
.empty{color:var(--muted);font-style:italic;padding:6px 0}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:30px}
@media(max-width:720px){.stats{grid-template-columns:repeat(2,1fr)}}
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
}
function toggleAll(open){
  document.querySelectorAll('.card details').forEach(d=>d.open=open);
}
document.getElementById('search').addEventListener('input', applyFilters);
document.querySelectorAll('.chip').forEach(c=>c.addEventListener('click',()=>setFilter(c.dataset.filter)));
document.getElementById('expand-all').addEventListener('click',()=>toggleAll(true));
document.getElementById('collapse-all').addEventListener('click',()=>toggleAll(false));
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
    "你是一名合规比对审核助手。你会看到一条外规条款和若干候选内规条款,"
    "请判断每个候选内规条款与外规条款是否「有一点关联」。\n"
    "- 判定为相关:候选与外规属于同一监管主题或同一合规领域、内容相互呼应、可能承接该外规要求。\n"
    "- 判定为不相关:候选与外规主体完全不同(例如外规是数据安全、候选是财务报销)、"
    "主题完全无关、内容毫无承接关系。\n"
    "宁可多留不可漏:只要有一点关联就保留,漏掉真正相关的候选比多显示一条不相关候选危害更大。\n"
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


def _cand_html(m: MatchResult) -> str:
    if not m.retrieved_candidates:
        return '<div class="empty">本条款没有可展示的检索召回(检索不可用,或全部召回经判定与本条款不相关)。</div>'
    parts = []
    for cand in m.retrieved_candidates:
        adopted = '<span class="adopted">✓ 采用</span>' if cand.adopted else ""
        score = f'<span class="score">得分 {cand.score:.4f}</span>' if cand.score is not None else ""
        parts.append(
            '<div class="cand">'
            f'<div class="cand-head"><span class="doc-badge">{html.escape(cand.doc_name)}</span>'
            f'<span class="label">{html.escape(cand.clause_label or cand.chunk_id)}</span>'
            f'<span class="backend-badge">{html.escape(cand.backend)}</span>{score}{adopted}</div>'
            f'<div class="snippet">{highlight(cand.snippet, cand.query)}</div>'
            '<details><summary>展开全文</summary>'
            f'<pre>{html.escape(cand.full_text)}</pre></details>'
            "</div>"
        )
    return "".join(parts)


def _card_html(m: MatchResult) -> str:
    badge_cls, status_text = _BADGE[m.status]
    clause_id = html.escape(m.external_article_id or m.external_clause_id)
    matched = ""
    if m.matched_internal_id and m.status != CoverageStatus.NOT_COVERED:
        matched = (
            '<div class="match">✓ 采用内规: '
            f'<b>{html.escape(m.matched_internal_document_name or "")}</b> '
            f'{html.escape(m.matched_internal_clause_number or "")}</div>'
            "<details><summary>承接内规原文</summary>"
            f"<pre>{html.escape(m.matched_internal_source_text)}</pre></details>"
        )
    remedy = ""
    if m.remedy_suggestion:
        remedy = (
            "<details><summary>整改建议</summary>"
            f"<pre>{html.escape(m.remedy_suggestion)}</pre></details>"
        )
    return (
        f'<section class="card" data-status="{m.status.value}">'
        '<div class="card-head">'
        f'<span class="clause-id">{clause_id}</span>'
        f'<span class="badge {badge_cls}">{status_text}</span>'
        "</div>"
        "<details><summary>外规原文</summary>"
        f"<pre>{html.escape(m.external_source_text)}</pre></details>"
        f'<div class="analysis"><b>判定分析:</b> {html.escape(m.analysis)}</div>'
        f"{matched}{remedy}"
        '<div class="recall"><h4>🔍 检索召回 top-k</h4>'
        f"{_cand_html(m)}"
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
    stats = {
        "全部": len(result.matches),
        "完全覆盖": result.fully_covered_count,
        "部分覆盖": result.partially_covered_count,
        "缺失": result.not_covered_count,
    }
    stats_cells = "".join(
        f'<div class="stat"><b>{n}</b><span>{k}</span></div>' for k, n in stats.items()
    )
    chips = "".join(
        f'<button class="chip chip-{cls}" data-filter="{f}"{" active" if f == "all" else ""}>'
        f"{label} ({count})</button>"
        for cls, f, label, count in (
            ("all", "all", "全部", len(result.matches)),
            ("full", CoverageStatus.FULLY_COVERED.value, "完全覆盖", result.fully_covered_count),
            ("part", CoverageStatus.PARTIALLY_COVERED.value, "部分覆盖", result.partially_covered_count),
            ("miss", CoverageStatus.NOT_COVERED.value, "缺失", result.not_covered_count),
        )
    )
    cards = "".join(_card_html(m) for m in result.matches) or '<p class="empty">无比对结果。</p>'

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
        f" · 覆盖率: {result.coverage_rate:.1%}</div>\n"
        f'<div class="stats">{stats_cells}</div>\n'
        f'<div class="chips">{chips}</div>\n'
        '<div class="controls">'
        '<input id="search" type="search" placeholder="按关键词筛选条款 / 召回片段…">'
        '<button id="expand-all">全部展开</button>'
        '<button id="collapse-all">全部收起</button>'
        "</div>\n"
        "</header>\n"
        f"<main>\n{cards}\n</main>\n"
        "<footer>「✓ 采用」= 该内规条款被比对 episode 采纳为承接条款;其余为批量预召回候选,供人工复核相关性。</footer>\n"
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
