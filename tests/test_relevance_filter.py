"""复核页候选相关性过滤回归 —— LLM 剔除完全不相关 top-k 候选,减少页面信息量。

背景:内规库很大一部分条款与外规并不相关(如数据安全外规 vs 财务报销内规),
仍以高分候选展示在复核页,信息量大。过滤后:adopted(= grounding 采用)候选
无条件保留;LLM 判定「有一点关联」的保留;完全不相关的剔除。任何失败(空内容 /
解析失败 / PingAn 散文错误)都 fail-open 保留全部,绝不误删。

测试自包含:假 LLM stub 记录 ainvoke 调用并返回固定 content,不依赖网络。
测试无 pytest-asyncio,同步测试里用 asyncio.run 调 async 过滤函数。
"""

from __future__ import annotations

import asyncio

from clause_splitter.compare.review import (
    _RELEVANCE_SYSTEM,
    _judge_candidates,
    filter_relevant_candidates,
)
from clause_splitter.models import CompareResult, CoverageStatus, MatchResult
from clause_splitter.models import RetrievedCandidate


def _cand(i: int, *, adopted: bool = False, doc: str = "内部制度") -> RetrievedCandidate:
    return RetrievedCandidate(
        chunk_id=f"内规#第{i}条",
        doc_name=doc,
        clause_label=f"第{i}条",
        backend="bm25",
        score=1.0,
        query="外规",
        snippet=f"候选片段 {i}",
        full_text=f"内部制度第{i}条全文:关于数据安全治理的具体要求 {i}",
        adopted=adopted,
    )


def _match(candidates: list[RetrievedCandidate], ext: str = "外规数据安全条款") -> MatchResult:
    return MatchResult(
        external_clause_id="外规#第1条",
        external_document_name="外部规定",
        external_article_id="第一条",
        external_source_text=ext,
        status=CoverageStatus.FULLY_COVERED,
        matched_internal_id="内规#第1条",
        matched_internal_document_name="内部制度",
        matched_internal_clause_number="第一条",
        matched_internal_source_text="内部承接条款",
        analysis="分析",
        remedy_suggestion="",
        retrieved_candidates=candidates,
    )


class _Resp:
    def __init__(self, content: str | None):
        self.content = content


class _FakeLLM:
    """记录 ainvoke 调用;按顺序弹出预置 content 返回。"""

    def __init__(self, contents: list[str | None]):
        self.contents = list(contents)
        self.calls: list[list[dict]] = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        if self.contents:
            return _Resp(self.contents.pop(0))
        return _Resp('{"related": []}')


def _run(coro):
    return asyncio.run(coro)


def _result(*matches: MatchResult) -> CompareResult:
    return CompareResult(
        external_documents=["外部规定"],
        internal_documents=["内部制度"],
        total_external_obligations=len(matches),
        total_internal_obligations=100,
        matches=list(matches),
    )


def test_judge_keeps_only_listed_indices():
    """LLM 判定的编号(1 起始 → 0 起始)在前;不足最少条数时按分数补齐。"""
    m = _match([_cand(0), _cand(1), _cand(2), _cand(3)])
    llm = _FakeLLM(['{"related": [2, 4]}'])
    kept = _run(_judge_candidates(llm, m))
    # 判定保留 idx1/idx3;为凑满最少 3 条,按得分补齐 idx0(同分稳定序)。
    assert [c.chunk_id for c in kept] == ["内规#第1条", "内规#第3条", "内规#第0条"]


def test_judge_keeps_adopted_regardless():
    """adopted(= grounding 采用)候选即使未被 LLM 列出也保留,且不因补齐丢失。"""
    cands = [_cand(0), _cand(1, adopted=True), _cand(2)]
    m = _match(cands)
    llm = _FakeLLM(['{"related": [1]}'])
    kept = _run(_judge_candidates(llm, m))
    assert [c.chunk_id for c in kept] == ["内规#第0条", "内规#第1条", "内规#第2条"]


def test_judge_fail_open_on_none_content():
    """content 为空(OpenAI provider 错误形态)→ 保留全部。"""
    m = _match([_cand(0), _cand(1)])
    llm = _FakeLLM([None])
    kept = _run(_judge_candidates(llm, m))
    assert len(kept) == 2


def test_judge_fail_open_on_prose_error():
    """非空但解析不到的散文串(PingAn provider 错误形态)→ 保留全部。

    PingAn 在 API 失败时返回 content="Error: Failed to get response from LLM.",
    不是 None——只有解析失败分支能兜住。
    """
    m = _match([_cand(0), _cand(1)])
    llm = _FakeLLM(["Error: Failed to get response from LLM."])
    kept = _run(_judge_candidates(llm, m))
    assert len(kept) == 2


def test_judge_fail_open_on_missing_or_bad_indices():
    """{} / 编号含 0 / 越界 → 保留全部,绝不误删。"""
    for content in ['{}', '{"related": [0, 5]}', '{"related": [1, 99]}']:
        m = _match([_cand(0), _cand(1)])
        llm = _FakeLLM([content])
        kept = _run(_judge_candidates(llm, m))
        assert len(kept) == 2, content


def test_filter_runs_per_clause_and_keeps_result():
    """2 条外规条款 → 2 次判定调用;各自保留判定相关项 + adopted,并按最少条数补齐;剔除数正确。"""
    m1 = _match([_cand(0), _cand(1)])  # related:[2] → idx1;补齐 idx0 → 2 条(全部候选)
    m2 = _match([_cand(0), _cand(1, adopted=True), _cand(2)])  # related:[3] → idx2 + adopted idx1;补齐 idx0 → 3 条
    result = _result(m1, m2)

    llm = _FakeLLM(['{"related": [2]}', '{"related": [3]}'])
    dropped = _run(filter_relevant_candidates(result, llm, concurrency=2))

    assert len(llm.calls) == 2
    assert [c.chunk_id for c in result.matches[0].retrieved_candidates] == ["内规#第1条", "内规#第0条"]
    assert [c.chunk_id for c in result.matches[1].retrieved_candidates] == ["内规#第1条", "内规#第2条", "内规#第0条"]
    # 净剔除:候选不足最少条数被补齐,最终展示数未减少。
    assert dropped == 0


def test_filter_noop_without_candidates_or_llm():
    """候选全空或 llm 为 None → 0 次调用、数据不动、不抛异常。"""
    empty = _result(_match([]))
    llm = _FakeLLM([])
    assert _run(filter_relevant_candidates(empty, llm)) == 0
    assert len(llm.calls) == 0

    has_cands = _result(_match([_cand(0)]))
    assert _run(filter_relevant_candidates(has_cands, None)) == 0
    assert [c.chunk_id for c in has_cands.matches[0].retrieved_candidates] == ["内规#第0条"]


def test_relevance_system_is_strict_binary():
    """过滤 prompt 是严格二分类:不再「宁可多留不可漏」,改为「宁可漏判不可误留」。"""
    assert "宁可多留不可漏" not in _RELEVANCE_SYSTEM
    assert "宁可漏判不可误留" in _RELEVANCE_SYSTEM
    assert "制度文件标题" in _RELEVANCE_SYSTEM  # 依据文件标题 + 条款内容
    assert "条款内容" in _RELEVANCE_SYSTEM
    assert "二分类" in _RELEVANCE_SYSTEM


def test_judge_prompt_carries_doc_title_and_clause_content():
    """判定 prompt 里每条候选带制度文件名 + 条款号 + 条款内容,供二分类参考。"""
    m = _match([_cand(0, doc="数据安全管理制度"), _cand(1, doc="财务报销制度")],
               ext="外规要求建立数据分类分级制度。")
    llm = _FakeLLM(['{"related": []}'])
    _run(_judge_candidates(llm, m))
    user = llm.calls[0][-1]["content"]
    assert "数据安全管理制度" in user
    assert "财务报销制度" in user
    assert "外规要求建立数据分类分级制度" in user
