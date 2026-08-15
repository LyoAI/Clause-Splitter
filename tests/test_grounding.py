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
from clause_splitter.compare.ground import ground_episode
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


def _mk_result(status, claimed_ids, evidence, seen_chunk_ids) -> AgentResult:
    """构造一个 agent episode:轨迹只返回 seen_chunk_ids,答案声称 claimed_ids。"""
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
            "analysis": "测试",
            "remedy_suggestion": "测试",
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
