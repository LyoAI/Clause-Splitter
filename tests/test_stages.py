"""Pipeline stage tests — applicability / split / internalization / chain skip."""

from __future__ import annotations

import json

import pytest

from clause_splitter.orchestrator import run_chain
from clause_splitter.pipeline import applicability, internalize, split_stage
from clause_splitter.pipeline.contexts import StageContext
from tests.fakes import CLAUSE_SPECS, PipelineFakeLLM, data_dir, fake_repos


def _ctx(reg: dict, llm, repos) -> StageContext:
    return StageContext(regulation=reg, llm=llm, repos=repos)


async def _mk_regulation(repos, tmp_path, name="外规测试办法.txt") -> dict:
    src = tmp_path / name
    # Five short articles keep regex-segmentation coverage above the 0.80 threshold.
    src.write_text(
        "第一条 金融机构应当建立数据分类分级制度。\n"
        "第二条 公司应当对重要数据加密存储。\n"
        "第三条 公司应当开展数据安全风险评估。\n"
        "第四条 公司应当加强员工数据安全培训。\n"
        "第五条 本办法自发布之日起施行。\n",
        encoding="utf-8",
    )
    return await repos.regulations.create({
        "title": "外规测试办法",
        "source_filename": name,
        "source_path": str(src),
        "applicability_applicable": None,
    })


# ── stage 1: applicability ────────────────────────────────────────────


async def test_applicability_yes(tmp_path):
    repos = fake_repos()
    reg = await _mk_regulation(repos, tmp_path)
    out = await applicability.run(_ctx(reg, PipelineFakeLLM(applicable=True), repos))
    assert out["applicability_applicable"] is True
    assert out["applicability_confidence"] == 0.9


async def test_applicability_fail_open_on_llm_error(tmp_path):
    repos = fake_repos()
    reg = await _mk_regulation(repos, tmp_path)
    out = await applicability.run(_ctx(reg, PipelineFakeLLM(applicable=None), repos))
    assert out["applicability_applicable"] is None
    assert "人工复核" in out["applicability_reason"]


async def test_applicability_fail_open_on_bad_json(tmp_path):
    repos = fake_repos()
    reg = await _mk_regulation(repos, tmp_path)

    class BadLLM(PipelineFakeLLM):
        async def ainvoke(self, messages, tools=None, **kwargs):
            from clause_splitter.llm.provider.base import LLMResponse

            return LLMResponse(content="<answer>not json</answer>", finish_reason="stop")

    out = await applicability.run(_ctx(reg, BadLLM(), repos))
    assert out["applicability_applicable"] is None


def test_parse_verdict_shapes():
    assert applicability.parse_verdict('<answer>{"applicable": "true", "confidence": "0.8", "reason": "r"}</answer>')
    bad = applicability.parse_verdict('<answer>{"applicable": false, "confidence": 1.5, "reason": ""}</answer>')
    assert bad["confidence"] == 1.0
    assert applicability.parse_verdict('<answer>{"confidence": 0.5}</answer>') is None


# ── stage 2: split ────────────────────────────────────────────────────


async def test_split_llm_fallback_creates_rows(tmp_path):
    with data_dir(tmp_path):
        repos = fake_repos()
        reg = await _mk_regulation(repos, tmp_path)
        out = await split_stage.run(_ctx(reg, None, repos))
        assert out["article_count"] == 5
        assert out["split_mode"] == "llm"
        rows, total = await repos.clauses.list_for_regulation(reg["_id"])
        assert total == 5
        assert rows[0]["clause_number"].startswith("第一")
        structure = tmp_path / "regulations" / reg["_id"] / "split" / "structure.json"
        assert structure.exists()
        assert len(json.loads(structure.read_text(encoding="utf-8"))["articles"]) == 5


async def test_split_empty_text_raises(tmp_path):
    repos = fake_repos()
    src = tmp_path / "empty.txt"
    src.write_text("   ", encoding="utf-8")
    reg = await repos.regulations.create({
        "title": "空文档", "source_filename": "empty.txt", "source_path": str(src),
    })
    from clause_splitter.pipeline.contexts import StageError

    with pytest.raises(StageError):
        await split_stage.run(_ctx(reg, None, repos))


# ── stage 3: internalization ──────────────────────────────────────────


async def test_internalize_updates_rows(tmp_path):
    repos = fake_repos()
    reg = await _mk_regulation(repos, tmp_path)
    await repos.clauses.replace_for_regulation(reg["_id"], [
        {"clause_number": "第一条", "clause_text": CLAUSE_SPECS[0]["full_text"]},
        {"clause_number": "第五条", "clause_text": CLAUSE_SPECS[4]["full_text"]},
    ])
    out = await internalize.run(_ctx(reg, PipelineFakeLLM(), repos))
    assert out["needs_internalization_count"] == 1
    assert out["no_internalization_count"] == 1
    rows, _ = await repos.clauses.list_for_regulation(reg["_id"])
    by_num = {r["clause_number"]: r for r in rows}
    assert by_num["第一条"]["internalization_decision"] == "需要内化"
    assert by_num["第五条"]["internalization_decision"] == "无需内化"


async def test_internalize_requires_clauses(tmp_path):
    repos = fake_repos()
    reg = await _mk_regulation(repos, tmp_path)
    from clause_splitter.pipeline.contexts import StageError

    with pytest.raises(StageError):
        await internalize.run(_ctx(reg, PipelineFakeLLM(), repos))


# ── chain orchestration ───────────────────────────────────────────────


async def test_chain_stops_when_not_applicable(tmp_path):
    repos = fake_repos()
    reg = await _mk_regulation(repos, tmp_path)
    out = await run_chain(_ctx(reg, PipelineFakeLLM(applicable=False), repos))
    assert "不适用" in out["chain_stopped"]
    doc = await repos.regulations.get(reg["_id"])
    assert doc["applicability_state"] == "done"
    assert doc["applicability_applicable"] is False
    for stage in ("split", "internalization", "analysis"):
        assert doc[f"{stage}_state"] == "skipped"


async def test_chain_stops_on_needs_review(tmp_path):
    repos = fake_repos()
    reg = await _mk_regulation(repos, tmp_path)
    out = await run_chain(_ctx(reg, PipelineFakeLLM(applicable=None), repos))
    assert "人工复核" in out["chain_stopped"]
    doc = await repos.regulations.get(reg["_id"])
    assert doc["applicability_applicable"] is None
    assert doc["split_state"] == "skipped"
