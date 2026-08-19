"""grounding 防幻觉回归 —— 注入伪造 ID 的 episode 必须被降级为缺失。

历史 bug:"批量 0% 覆盖" —— agent 声称已覆盖,但引用的内规条款 ID 不在
检索轨迹里。这三道防线的核心是 ``ground_episode``:每个 claimed id 必须
∈ seen 集 且 ∈ store,任一不落地即丢弃;全部不落地 → 降级 缺失。

测试自包含:用内存 DocumentStore 造内规库,不依赖磁盘真实数据。
"""

from __future__ import annotations

import json

from clause_splitter.agent import AgentResult, AgentTurn
from clause_splitter.compare.episode import ExternalClause
from clause_splitter.compare.ground import _segment_label, ground_episode
from clause_splitter.models import CoverageStatus
from clause_splitter.tools.searcher.store import Chunk, DocumentStore

EXT = ExternalClause(document_name="外规", clause_number="第一条", full_text="测试条款")

REAL_ID = "网络数据安全管理条例#第一条"
FAKE_ID = "网络数据安全管理条例#不存在条款"


def _mini_store() -> DocumentStore:
    """内存小内规库,供 grounding 校验 claim 是否 ∈ store。"""
    store = DocumentStore(name="internal")
    store.add_chunk(Chunk(
        chunk_id=REAL_ID, doc_id="网络数据安全管理条例", order=0,
        text="数据处理者应当建立数据安全管理制度。",
        meta={"doc": "网络数据安全管理条例", "clause_number": "第一条", "section": "第一章 总则"},
    ))
    store.add_chunk(Chunk(
        chunk_id="网络数据安全管理条例#第二条", doc_id="网络数据安全管理条例", order=1,
        text="禁止未经授权的数据访问和数据泄露。",
        meta={"doc": "网络数据安全管理条例", "clause_number": "第二条", "section": "第一章 总则"},
    ))
    return store


def _mk_result(status, claimed_ids, evidence, seen_chunk_ids, analysis="测试", remedy="测试",
               extra: dict | None = None) -> AgentResult:
    """构造一个 agent episode:轨迹只返回 seen_chunk_ids,答案声称 claimed_ids。

    ``extra`` 可并入答案 JSON(覆盖/新增键),用于测试内化判定等新字段透传。
    """
    trajectory = [
        AgentTurn(
            step=i,
            tool_name="bm25_search",
            tool_args={"query": "x"},
            tool_result=json.dumps([{"chunk_id": cid, "score": 1.0}], ensure_ascii=False),
            tool_time_s=0.1,
        )
        for i, cid in enumerate(seen_chunk_ids)
    ]
    answer_body = json.dumps(
        {
            "status": status,
            "matched_internal_clause_ids": claimed_ids,
            "evidence": {k: v for k, v in evidence.items()},
            "analysis": analysis,
            "remedy_suggestion": remedy,
            **(extra or {}),
        },
        ensure_ascii=False,
    )
    raw = f"<answer>{answer_body}</answer>"
    return AgentResult(question="q", answer=answer_body, unit="", raw_answer=raw, trajectory=trajectory)


def test_fake_claimed_id_downgrades_to_missing():
    """声称完全覆盖但引用的全是伪造 ID → 降级为缺失,analysis 替换为降级文案。"""
    store = _mini_store()

    r = ground_episode(_mk_result("完全覆盖", [FAKE_ID], {FAKE_ID: "quote"}, [REAL_ID]), store, EXT)
    assert r["status"] == CoverageStatus.NOT_COVERED
    assert "降级" in r["analysis"]
    assert r["matched_internal_id"] is None


def test_real_id_outside_seen_set_downgrades():
    """引用真实存在的条款但不在检索轨迹(seen 集)里 → 同样降级。"""
    store = _mini_store()

    r = ground_episode(_mk_result("部分覆盖", [REAL_ID], {REAL_ID: "q"}, [FAKE_ID]), store, EXT)
    assert r["status"] == CoverageStatus.NOT_COVERED


def test_groundable_claim_keeps_coverage():
    """真实条款同时在 seen 集与 store 中 → 保留覆盖判定,回填 store 原文。"""
    store = _mini_store()

    r = ground_episode(_mk_result("部分覆盖", [REAL_ID], {REAL_ID: "原文"}, [REAL_ID]), store, EXT)
    assert r["status"] == CoverageStatus.PARTIALLY_COVERED
    assert r["matched_internal_id"] == REAL_ID
    assert r["matched_internal_source_text"] == "数据处理者应当建立数据安全管理制度。"  # store 原文,非 agent 改写


def test_missing_status_passes_through():
    """status=缺失 不需 grounding,直接通过。"""
    store = _mini_store()

    r = ground_episode(_mk_result("缺失", [], {}, [REAL_ID]), store, EXT)
    assert r["status"] == CoverageStatus.NOT_COVERED
    assert r["matched_internal_id"] is None


def test_not_covered_empty_analysis_falls_back():
    """agent 判定缺失但 analysis 为空 → 回填缺省文案,MatchResult 校验不炸。

    历史 bug:MatchResult.analysis 要求 min_length=1,空串直接 ValidationError,
    且该异常发生在 aggregate 的 try/except 之外,拖垮整批比对。
    """
    store = _mini_store()

    r = ground_episode(_mk_result("缺失", [], {}, [], analysis="", remedy=""), store, EXT)
    assert r["status"] == CoverageStatus.NOT_COVERED
    assert r["analysis"].strip()  # 回填缺省文案,非空

    from clause_splitter.models import MatchResult

    m = MatchResult(**r)  # 不再抛 validation error
    assert m.analysis


def test_grounded_empty_analysis_still_nonempty():
    """真实承接但 analysis 为空 → 合并承接条款后非空,且不以换行开头。"""
    store = _mini_store()

    r = ground_episode(
        _mk_result("完全覆盖", [REAL_ID], {REAL_ID: "原文"}, [REAL_ID], analysis=""),
        store, EXT,
    )
    assert r["analysis"].strip()
    assert r["analysis"].startswith("承接内规条款:")


# ── _segment_label 编号修正回归 ─────────────────────────────────────────


def _chunk(chunk_id: str, text: str, section: str = "", meta: dict | None = None) -> Chunk:
    m = {"section": section, **(meta or {})}
    return Chunk(chunk_id=chunk_id, doc_id=chunk_id.split("#")[0], order=0, text=text, meta=m)


def test_segment_label_excel_leaf_no_separator():
    """Excel 内规库 section 直接是 "第一条"(无 ` > ` 分隔符) → 返回 "第一条",
    而不是 0 起始的 chunk 序号(历史偏移 bug)。"""
    assert _segment_label(_chunk("网络数据安全管理条例#0", "第一条 数据安全…", section="第一条")) == "第一条"
    assert _segment_label(_chunk("网络数据安全管理条例#1", "第二条 数据出境…", section="第二条")) == "第二条"


def test_segment_label_path_takes_last_leaf():
    """章节路径取末级标题。"""
    assert _segment_label(_chunk("doc#0", "第三条…", section="第一章 总则 > 第三条")) == "第三条"


def test_segment_label_no_section_extracts_article_from_text():
    """无 section 时从原文开头提取 "第X条"。"""
    assert _segment_label(_chunk("doc#5", "第三条　处理者应当…", section="")) == "第三条"
    assert _segment_label(_chunk("doc#5", "第二十条　…", section="")) == "第二十条"


def test_segment_label_fallback_is_1based():
    """既无 section 也无条款号 → 回退 1 起始段落号(第N段),不再显示 0。"""
    assert _segment_label(_chunk("doc#0", "无编号的一段文字", section="")) == "第1段"
    assert _segment_label(_chunk("doc#8", "无编号的一段文字", section="")) == "第9段"


# ── 内化判定(前置)/ 差异说明(episode) 透传 ─────────────────────────────


def _ext_with_intl(**kw) -> ExternalClause:
    """带前置内化判定的外规条款(模拟检索前已由 pre_internalize 填充字段)。"""
    return ExternalClause(
        document_name="外规", clause_number="第一条", full_text="测试条款",
        clause_type=kw.get("clause_type", "义务性条款"),
        internalization_decision=kw.get("internalization_decision", "需要内化"),
        internalization_reason=kw.get("internalization_reason", "前置判定依据"),
    )


def test_ground_episode_passes_through_pre_classified_internalization():
    """内化判定来自前置阶段(存于 ext),原样透传到 MatchResult;差异说明仍由 episode 产出。"""
    store = _mini_store()
    r = ground_episode(
        _mk_result(
            "部分覆盖", [REAL_ID], {REAL_ID: "原文"}, [REAL_ID],
            extra={
                # episode 契约已移除内化字段:即使 agent 误输出,也不该被采纳。
                "clause_type": "宣示性条款",
                "internalization_decision": "无需内化",
                "difference_analysis": "外规要求加密+访问控制;内规仅覆盖加密,遗漏访问控制。",
            },
        ),
        store, _ext_with_intl(
            clause_type="义务性条款", internalization_decision="需要内化",
            internalization_reason="外规为义务性条款且涉及核心业务",
        ),
    )
    # 前置判定优先级高于 episode 输出。
    assert r["clause_type"] == "义务性条款"
    assert r["internalization_decision"] == "需要内化"
    assert r["internalization_reason"] == "外规为义务性条款且涉及核心业务"
    # 差异说明仍来自 episode(部分覆盖必须写差异)。
    assert r["difference_analysis"].startswith("外规要求加密")
    # 覆盖判定本身不受新字段影响
    assert r["status"] == CoverageStatus.PARTIALLY_COVERED


def test_ground_episode_missing_new_fields_defaults_empty():
    """未做前置判定(ext 字段空)/旧答案 → 内化字段回空串,MatchResult 校验不炸。"""
    store = _mini_store()
    r = ground_episode(_mk_result("缺失", [], {}, [], analysis="缺失原因", remedy=""), store, EXT)
    assert r["clause_type"] == ""
    assert r["internalization_decision"] == ""
    assert r["internalization_reason"] == ""
    assert r["difference_analysis"] == ""
    from clause_splitter.models import MatchResult

    m = MatchResult(**r)
    assert m.clause_type == "" and m.internalization_decision == "" and m.difference_analysis == ""


def test_ensure_adopted_in_candidates_injects_grounded_chunks():
    """采用条款不在批量预召回 top-k 里时,强制补入候选(adopted=True,不重复)。"""
    from clause_splitter.compare.aggregate import _ensure_adopted_in_candidates
    from clause_splitter.models import RetrievedCandidate

    store = DocumentStore(name="inject-test")
    store.add_chunk(Chunk(
        chunk_id="办法#0", doc_id="数据安全管理办法", order=0,
        text="第一条　本制度适用于数据分类分级。",
        meta={"section": "第一章 总则 > 第一条"},
    ))
    # 批量预召回只有一条无关候选;采用条款「办法#0」不在其中。
    candidates = [
        RetrievedCandidate(
            chunk_id="办法#1", doc_name="数据安全管理办法", clause_label="第二条",
            backend="bm25", score=5.0, query="q",
            snippet="第二条　重要数据加密。", full_text="第二条　重要数据加密。",
        ),
    ]
    _ensure_adopted_in_candidates(candidates, {"办法#0"}, store, "q")
    ids = [c.chunk_id for c in candidates]
    assert "办法#0" in ids  # 强制补入
    adopted = next(c for c in candidates if c.chunk_id == "办法#0")
    assert adopted.adopted is True
    assert adopted.backend == "episode"  # 来自比对检索而非批量预召回
    assert adopted.score is None  # 无批量召回得分
    assert adopted.doc_name == "数据安全管理办法"
    assert adopted.clause_label == "第一条"  # 走 _segment_label 的 1 起始编号
    # 已在候选列表里的采用条款不重复补入。
    before = len(candidates)
    _ensure_adopted_in_candidates(candidates, {"办法#0"}, store, "q")
    assert len(candidates) == before


def test_ground_episode_normalizes_preventive_to_need():
    """旧数据 ext 填了「预防性内化」→ 透传时两值化为「需要内化」。"""
    store = _mini_store()
    r = ground_episode(
        _mk_result("缺失", [], {}, []),
        store, _ext_with_intl(internalization_decision="预防性内化"),
    )
    assert r["internalization_decision"] == "需要内化"
    assert r["clause_type"] == "义务性条款"  # 其余字段原样透传


def test_ground_episode_tolerates_non_str_diff_analysis():
    """episode 把 difference_analysis 输出成非字符串(如 None/数字) → 回空串,不抛异常。"""
    store = _mini_store()
    r = ground_episode(
        _mk_result("缺失", [], {}, [], extra={"difference_analysis": 123}),
        store, EXT,
    )
    assert r["difference_analysis"] == ""
    # 前置判定字段即使 ext 填了非 str 也宽容回空串(防脏数据)。
    r2 = ground_episode(
        _mk_result("缺失", [], {}, []),
        store, _ext_with_intl(clause_type=123, internalization_decision=None),
    )
    assert r2["clause_type"] == ""
    assert r2["internalization_decision"] == ""
