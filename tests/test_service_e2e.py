"""Service-level e2e — upload → full chain (4 stages) → artifacts, all offline.

Runs the REAL chain orchestration, split fallback, internalization, episodes
(ReAct loop + grounding) and review-page render against the fake LLM +
substring searcher + in-memory Mongo.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from api.base_fastapi import ApiError
from clause_splitter.services import context as services
from clause_splitter.services import regulation_service
from clause_splitter.jobs import JobRunner
from tests.fakes import (
    CLAUSE_SPECS,
    PipelineFakeLLM,
    build_rag_service,
    data_dir,
    fake_repos,
    patch_rag,
)

SOURCE_TEXT = "\n".join(
    f"{spec['clause_number']}　{spec['full_text']}" for spec in CLAUSE_SPECS
) + "\n"


async def _wait(repos, job_id: str, timeout_s: float = 60.0) -> dict:
    for _ in range(int(timeout_s / 0.1)):
        job = await repos.jobs.get(job_id)
        if job["state"] in ("done", "failed"):
            return job
        await asyncio.sleep(0.1)
    raise AssertionError("chain job did not finish in time")


async def test_full_chain_e2e(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        repos = fake_repos()
        llm = PipelineFakeLLM(applicable=True)
        runner = JobRunner(repos, workers=1, llm_factory=lambda: llm)
        services.init(repos, runner)
        patch_rag(monkeypatch, build_rag_service())
        await runner.start()

        # Keep the split stage on the deterministic text-fallback path.
        import clause_splitter.pipeline.split_stage as split_stage_mod

        async def _no_remote_parse(src, llm, service=None):
            raise RuntimeError("block parsing disabled in tests")

        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_remote_parse)

        try:
            reg, created = await regulation_service.upload(
                SOURCE_TEXT.encode("utf-8"),
                "金融数据安全管理办法.txt",
                "金融数据安全管理办法",
                repos=repos,
            )
            assert created and reg["title"] == "金融数据安全管理办法"

            # sha256 dedupe returns the same row.
            _reg2, created2 = await regulation_service.upload(
                SOURCE_TEXT.encode("utf-8"), "金融数据安全管理办法.txt", "", repos=repos
            )
            assert not created2 and _reg2["_id"] == reg["_id"]

            # Precondition: split before applicability → 409 (ApiError).
            with pytest.raises(ApiError) as exc:
                await regulation_service.start_stage(reg["_id"], "split")
            assert exc.value.status_code == 409

            job = await regulation_service.start_stage(reg["_id"], "chain")
            done = await _wait(repos, job["_id"])
            assert done["state"] == "done", done["error"]

            # ── regulation ────────────────────────────────────────────
            doc = await repos.regulations.get(reg["_id"])
            assert doc["applicability_applicable"] is True
            for stage in ("applicability", "split", "internalization", "analysis"):
                assert doc[f"{stage}_state"] == "done", stage
            assert doc["article_count"] == len(CLAUSE_SPECS)
            summary = doc["analysis_summary"]
            # 4 internalizable clauses: 1 full + 1 partial → coverage 0.5.
            assert summary["fully_covered_count"] == 1
            assert summary["partially_covered_count"] == 1
            assert summary["no_internalization_count"] == 1
            assert set(summary) == {
                "fully_covered_count",
                "partially_covered_count",
                "missing_count",
                "internalizable_count",
                "no_internalization_count",
            }
            assert doc["analysis_index_signature"]

            # ── clause rows ───────────────────────────────────────────
            rows, total = await repos.clauses.list_for_regulation(reg["_id"])
            assert total == len(CLAUSE_SPECS)
            by_num = {r["clause_number"]: r for r in rows}
            first = by_num["第一条"]
            assert first["internalization_decision"] == "需要内化"
            assert first["status"] == "完全覆盖"
            assert first["matched_internal_id"] == "数据安全管理办法#0"
            assert first["candidates"], "covered clause must carry display candidates"
            assert by_num["第二条"]["remedy_suggestion"]
            assert by_num["第二条"]["difference_analysis"]
            fifth = by_num["第五条"]
            assert fifth["internalization_decision"] == "无需内化"
            assert "不纳入覆盖比对" in fifth["analysis"]

            # ── artifacts ─────────────────────────────────────────────
            reg_dir = tmp_path / "regulations" / reg["_id"]
            result = json.loads((reg_dir / "compare_result.json").read_text(encoding="utf-8"))
            assert len(result["matches"]) == len(CLAUSE_SPECS)
            assert (reg_dir / "内规检索复核.html").exists()
            episodes = list((reg_dir / "episodes").glob("*.json"))
            # One trajectory per internalized clause (4).
            assert len(episodes) == 4
            assert (reg_dir / "split" / "structure.json").exists()

            # ── stage calls observed on the fake LLM ──────────────────
            assert "applicability" in llm.calls
            assert "doc_profile" in llm.calls
            assert "internalization" in llm.calls
        finally:
            await runner.stop()
            services.reset()


async def test_stage_preconditions_and_reset(tmp_path):
    with data_dir(tmp_path):
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: PipelineFakeLLM())
        services.init(repos, runner)
        await runner.start()
        try:
            reg, _ = await regulation_service.upload("第一条 测试。".encode("utf-8"), "t.txt", "t", repos=repos)
            # split blocked until applicability ran
            with pytest.raises(ApiError):
                await regulation_service.start_stage(reg["_id"], "split")
            # internalization blocked until split ran
            with pytest.raises(ApiError):
                await regulation_service.start_stage(reg["_id"], "internalization")
            # analysis blocked until internalization ran
            with pytest.raises(ApiError):
                await regulation_service.start_stage(reg["_id"], "analysis")

            # Applicability runs → split unblocked (applicable=True).
            await regulation_service.override_applicability(reg["_id"], True, "人工确认")
            doc = await repos.regulations.get(reg["_id"])
            assert doc["applicability_state"] == "done"
            job = await regulation_service.start_stage(reg["_id"], "split")
            done = await _wait(repos, job["_id"], timeout_s=30)
            assert done["state"] == "done", done["error"]

            # Re-running split is allowed (predecessor done); internalization still blocked? no — split done now.
            with pytest.raises(ApiError):
                await regulation_service.start_stage(reg["_id"], "analysis")
        finally:
            await runner.stop()
            services.reset()
