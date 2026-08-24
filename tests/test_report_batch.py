"""差距分析报告批处理回归 —— 子 agent 分 batch 总结 + 最终模型汇总。

历史问题:``generate_gap_report`` 把全部匹配明细 + 检索候选全文 + 整个内规库
一次性拼进 LLM user prompt,轻易超上下文窗口。批处理后:
- 每个批总结调用只吃该批条款(数量 = REPORT_BATCH_SIZE),单次输入远小于窗口;
- details 排除 ``retrieved_candidates``(复核页专用,SKILL 从不引用,是"中间搜索
  结果超长"的元凶;候选数据本身仍保留在 matches / compare_result.json / HTML);
- 最终模型只拿确定性统计 + 各批总结,不再接触原始 matches / 候选 / 整库。

测试自包含:假 LLM stub 记录每次 invoke 的 messages 并返回固定内容,不依赖网络。
"""

from __future__ import annotations

from clause_splitter.compare.report import (
    _BATCH_SUMMARY_SYSTEM,
    _batches,
    _build_batch_prompt,
    generate_gap_report,
)
from clause_splitter.models import CompareResult, CoverageStatus, MatchResult
from clause_splitter.models.compare import RetrievedCandidate


def _mk_match(i: int, status: CoverageStatus = CoverageStatus.FULLY_COVERED) -> MatchResult:
    """构造第 i 条比对结果;带一条复核候选,验证批 prompt 里被排除。"""
    covered = status != CoverageStatus.NOT_COVERED
    return MatchResult(
        external_clause_id=f"外规#第{i}条",
        external_document_name="外部规定",
        external_article_id=f"第{i}条",
        external_source_text=f"外部要求 {i}",
        status=status,
        matched_internal_id=f"内规#第{i}条" if covered else None,
        matched_internal_document_name="内部制度" if covered else None,
        matched_internal_clause_number=f"第{i}条" if covered else "",
        matched_internal_source_text=f"内部承接条款 {i}" if covered else "",
        analysis="比对分析",
        core_regulatory_requirements="1）、建立数据分类分级制度",
        retrieved_candidates=[
            RetrievedCandidate(
                chunk_id=f"内规#第{i}条",
                doc_name="内部制度",
                clause_label=f"第{i}条",
                snippet="候选片段",
                full_text="候选全文很长很长",
            )
        ],
    )


def _mk_result(n: int) -> CompareResult:
    """构造 n 条比对结果(第 0..n-1 条完全覆盖,第 n-1 条缺失,便于计数)。"""
    matches = [_mk_match(i) for i in range(n - 1)]
    matches.append(_mk_match(n - 1, CoverageStatus.NOT_COVERED))
    return CompareResult(
        external_documents=["外部规定"],
        internal_documents=["内部制度"],
        total_external_obligations=n,
        total_internal_obligations=100,
        matches=matches,
    )


class _Resp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """记录每次 invoke 的 messages;批调用返回批总结,最终调用返回完整报告。"""

    def __init__(self, final_report: str = "最终报告正文"):
        self.final_report = final_report
        self.calls: list[list[dict]] = []

    def invoke(self, messages: list[dict]) -> _Resp:
        self.calls.append(messages)
        system = next(m["content"] for m in messages if m["role"] == "system")
        if system == _BATCH_SUMMARY_SYSTEM:
            return _Resp("批总结素材")
        return _Resp(self.final_report)


def _batch_calls(llm: _FakeLLM) -> list[dict]:
    """返回所有批总结调用(以 system 是否为 _BATCH_SUMMARY_SYSTEM 区分)。"""
    return [
        messages
        for messages in llm.calls
        if next(m["content"] for m in messages if m["role"] == "system") == _BATCH_SUMMARY_SYSTEM
    ]


def _user_of(messages: list[dict]) -> str:
    return next(m["content"] for m in messages if m["role"] == "user")


def test_batches_split():
    rows = list(range(25))
    chunks = _batches(rows, 10)
    assert len(chunks) == 3
    assert [len(c) for c in chunks] == [10, 10, 5]
    assert chunks[0][0] == 0 and chunks[2][-1] == 24


def test_batch_prompt_scoped():
    """批 prompt 只含该批条款;details 排除候选全文;不出现整库/他批条款。"""
    result = _mk_result(3)
    prompt = _build_batch_prompt(result, result.matches[:2])

    # 只含本批条款 id,不含第三批。
    assert "外规#第0条" in prompt and "外规#第1条" in prompt
    assert "外规#第2条" not in prompt
    # 候选全文(中间搜索结果)不进批 prompt。
    assert "retrieved_candidates" not in prompt
    assert "候选全文" not in prompt
    # 承接内规只列本批 grounding 实际承接的条款(去重后两条)。
    assert "内规#第0条" in prompt and "内规#第1条" in prompt
    # 核心监管要求(新契约字段)进入批 prompt,供报告撰写者引用。
    assert "1）、建立数据分类分级制度" in prompt


def test_generate_gap_report_batched(monkeypatch):
    """25 条、批大小 10 → 3 次批总结 + 1 次最终调用,报告为最终模型产物。"""
    monkeypatch.setattr("clause_splitter.compare.report.REPORT_BATCH_SIZE", 10)
    result = _mk_result(25)
    llm = _FakeLLM(final_report="按 Skill 撰写的完整报告")

    out = generate_gap_report(result, store=None, llm=llm, output_dir="")

    assert out["mode"] == "llm"
    assert out["report"] == "按 Skill 撰写的完整报告"

    batch_msgs = _batch_calls(llm)
    assert len(batch_msgs) == 3  # 批总结调用数 == 批数
    assert len(llm.calls) == 4  # 3 批 + 1 最终

    # 每批只装该批条款:第 21 条只出现在第 3 批,不出现第 1 批。
    b1, b3 = _user_of(batch_msgs[0]), _user_of(batch_msgs[2])
    assert "外规#第21条" in b3 and "外规#第21条" not in b1
    # 候选全文不进任何批 prompt。
    assert all("retrieved_candidates" not in _user_of(m) for m in batch_msgs)

    # 最终调用:system 是 SKILL.md,user 只有统计 + 批次总结,无原始条款明细。
    final_msgs = llm.calls[-1]
    final_system = next(m["content"] for m in final_msgs if m["role"] == "system")
    assert final_system != _BATCH_SUMMARY_SYSTEM  # 是 SKILL.md
    final_user = _user_of(final_msgs)
    assert "## 批次 1" in final_user and "## 批次 3" in final_user
    assert "整体统计" in final_user
    assert "外规#第0条" not in final_user  # 不重复原始条款明细
    assert "retrieved_candidates" not in final_user
    # 确定性统计不含覆盖率(延续"报告不写覆盖率")。
    assert "覆盖率" not in final_user


def test_batch_failure_falls_back_template():
    """某个批总结调用抛异常 → 整体回退模板报告,全部条款仍逐行呈现。"""
    result = _mk_result(25)

    class _ExplodingLLM(_FakeLLM):
        def invoke(self, messages):
            self.calls.append(messages)
            raise RuntimeError("batch 失败")

    bad = _ExplodingLLM()
    out = generate_gap_report(result, store=None, llm=bad, output_dir="")

    assert out["mode"] == "template"
    assert "完全覆盖" in out["report"]
    assert "缺失" in out["report"]
    assert "外规#第24条" in out["report"]  # 模板逐行呈现全部条款
    # 模板回退报告带上内化判定总览 + 各节内化判定列(空字段 → 待判定)
    assert "内化判定总览" in out["report"]
    assert "| 内化判定 | 条款数 | 占比 | 说明 |" in out["report"]
    assert "待判定" in out["report"]


def test_to_summary_dict_excludes_candidates():
    """to_summary_dict 的 details 排除候选全文;候选本身仍保留在 matches 里。"""
    result = _mk_result(2)
    d = result.to_summary_dict()
    assert all("retrieved_candidates" not in item for item in d["details"])
    assert all(item["external_clause_id"].startswith("外规#") for item in d["details"])
    # 搜索结果(候选)在 MatchResult 上原样保留。
    assert result.matches[0].retrieved_candidates
