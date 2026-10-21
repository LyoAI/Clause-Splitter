"""Backend push integration — manual/auto push, download fallback, callback."""

from __future__ import annotations

import asyncio

import pytest

import config.config_app as config_app_mod
from api.base_fastapi import ApiError
from clause_splitter.jobs import JobRunner
from clause_splitter.services import context as services
from clause_splitter.services import integration_service
import clause_splitter.pipeline.split_stage as split_stage_mod
from tests.fakes import (
    CLAUSE_SPECS,
    PipelineFakeLLM,
    build_store,
    data_dir,
    fake_repos,
    patch_registry,
)

SOURCE_TEXT = "\n".join(
    f"{spec['clause_number']}　{spec['full_text']}" for spec in CLAUSE_SPECS
) + "\n"


class NoApplicabilityLLM(PipelineFakeLLM):
    """Manual pushes must never reach the stage-1 applicability LLM call."""

    async def ainvoke(self, messages, tools=None, **kwargs):
        if "是否适用于平安科技" in messages[0]["content"]:
            raise RuntimeError("applicability must be skipped on manual push")
        return await super().ainvoke(messages, tools, **kwargs)


def _no_mineru(path, out_dir=""):
    raise RuntimeError("MinerU disabled in tests")


async def _wait(repos, job_id: str, timeout_s: float = 60.0) -> dict:
    for _ in range(int(timeout_s / 0.1)):
        job = await repos.jobs.get(job_id)
        if job["state"] in ("done", "failed"):
            return job
        await asyncio.sleep(0.1)
    raise AssertionError("chain job did not finish in time")


async def test_manual_push_runs_chain_and_calls_back(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        repos = fake_repos()
        llm = NoApplicabilityLLM()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: llm)
        services.init(repos, runner)
        integration_service.register(runner)
        store = build_store()
        patch_registry(monkeypatch, store)

        async def pinned_store(ctx):
            return store

        monkeypatch.setattr(runner, "_pinned_store", pinned_store)
        monkeypatch.setattr(split_stage_mod, "clauses_from_document", _no_mineru)
        object.__setattr__(config_app_mod.CONFIG_APP, "backend_callback_url", "http://cb")
        delivered: list[dict] = []
        monkeypatch.setattr(
            integration_service, "_post_callback",
            lambda url, payload: delivered.append(payload),
        )
        await runner.start()
        try:
            result = await integration_service.handle_push({"args": {
                "OptType": 0, "docContent": SOURCE_TEXT, "docType": "3",
                "docId": "doc1", "taskId": "task1", "uid": "u1",
            }})
            assert result["manual"] is True and result["task_id"] == "task1"
            done = await _wait(repos, result["job_id"])
            assert done["state"] == "done", done["error"]

            reg = await repos.regulations.get(result["regulation_id"])
            assert reg["applicability_state"] == "done"
            assert reg["applicability_applicable"] is True
            assert "手动" in reg["applicability_reason"]
            assert reg["push_doc_id"] == "doc1" and reg["push_task_id"] == "task1"
            assert "applicability" not in llm.calls

            assert len(delivered) == 1
            payload = delivered[0]
            assert payload["args"]["taskId"] == "task1"
            assert payload["status"] == "done"
            assert payload["applicable"] is True
            assert len(payload["clauses"]) == len(CLAUSE_SPECS)
        finally:
            await runner.stop()
            services.reset()
            object.__setattr__(config_app_mod.CONFIG_APP, "backend_callback_url", "")


async def test_auto_push_downloads_from_url(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: PipelineFakeLLM())
        services.init(repos, runner)
        try:
            monkeypatch.setattr(integration_service, "_fetch", lambda url: SOURCE_TEXT.encode("utf-8"))
            result = await integration_service.handle_push({"args": {
                "OptType": 1, "docType": "3", "docId": "doc2",
                "doclobsUrl": "http://iobs.example/download/abc?attname=测试外规管理办法",
                "taskId": "task2", "uid": "u2",
            }})
            assert result["manual"] is False and result["duplicate"] is False

            reg = await repos.regulations.get(result["regulation_id"])
            assert reg["title"] == "测试外规管理办法"
            assert reg["source_filename"].endswith(".pdf")  # attname has no suffix → default
            assert reg["push_opt_type"] == 1
            job = await repos.jobs.get(result["job_id"])
            assert job["state"] == "queued" and job["job_type"] == "chain"
        finally:
            await runner.stop()
            services.reset()


async def test_push_without_content_or_url_rejected(tmp_path):
    with data_dir(tmp_path):
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: PipelineFakeLLM())
        services.init(repos, runner)
        try:
            with pytest.raises(ApiError) as exc:
                await integration_service.handle_push({"args": {"docId": "d", "taskId": "t"}})
            assert exc.value.status_code == 400
            with pytest.raises(ApiError):
                await integration_service.handle_push({"args": {"taskId": "t"}})
        finally:
            await runner.stop()
            services.reset()
