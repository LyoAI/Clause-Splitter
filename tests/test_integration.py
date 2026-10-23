"""Backend push integration — manual/auto push, download fallback, callback."""

from __future__ import annotations

import asyncio

import pytest

import config.config_app as config_app_mod
from api.base_fastapi import ApiError
from clause_splitter import config as cs_config
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
        monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "immediate")
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


async def test_flat_body_auto_push_waits_for_cron_then_runs(tmp_path, monkeypatch):
    """Flat (unwrapped) body + auto dispatch: OptType=1 parks in scheduled
    until drain_scheduled admits it; the chain then runs and build_result
    serves the payload by docId."""
    with data_dir(tmp_path):
        repos = fake_repos()
        llm = PipelineFakeLLM()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: llm)
        services.init(repos, runner)
        integration_service.register(runner)
        store = build_store()
        patch_registry(monkeypatch, store)

        async def pinned_store(ctx):
            return store

        monkeypatch.setattr(runner, "_pinned_store", pinned_store)
        monkeypatch.setattr(split_stage_mod, "clauses_from_document", _no_mineru)
        await runner.start()
        try:
            # Flat field map (no "args" wrapper) — spec shape pending.
            result = await integration_service.handle_push({
                "OptType": 1, "docContent": SOURCE_TEXT, "docType": "3",
                "docId": "doc3", "taskId": "task3", "uid": "u3",
            })
            assert result["dispatch"] == "scheduled"
            job = await repos.jobs.get(result["job_id"])
            assert job["state"] == "scheduled"
            # docId pull before admission → reflects the scheduled job
            early = await integration_service.build_result("doc3")
            assert early["status"] == "scheduled"

            assert await runner.drain_scheduled() == 1
            done = await _wait(repos, result["job_id"])
            assert done["state"] == "done", done["error"]

            payload = await integration_service.build_result("doc3")
            assert payload["args"]["docId"] == "doc3"
            assert payload["status"] == "done"
            assert len(payload["clauses"]) == len(CLAUSE_SPECS)
        finally:
            await runner.stop()
            services.reset()


async def test_build_result_unknown_docid_404(tmp_path):
    with data_dir(tmp_path):
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: PipelineFakeLLM())
        services.init(repos, runner)
        try:
            with pytest.raises(ApiError) as exc:
                await integration_service.build_result("no-such-doc")
            assert exc.value.status_code == 404
        finally:
            await runner.stop()
            services.reset()


async def test_cron_mode_schedules_even_manual_pushes(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "cron")
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: PipelineFakeLLM())
        services.init(repos, runner)
        try:
            result = await integration_service.handle_push({"args": {
                "OptType": 0, "docContent": SOURCE_TEXT, "docId": "doc4", "taskId": "t4",
            }})
            assert result["dispatch"] == "scheduled"
            job = await repos.jobs.get(result["job_id"])
            assert job["state"] == "scheduled"
        finally:
            await runner.stop()
            services.reset()


def test_dispatch_mode_matrix(monkeypatch):
    from clause_splitter.services.dispatcher import dispatch_mode

    monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "auto")
    assert dispatch_mode(manual=True) == "immediate"
    assert dispatch_mode(manual=False) == "scheduled"
    monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "cron")
    assert dispatch_mode(manual=True) == "scheduled"
    monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "immediate")
    assert dispatch_mode(manual=False) == "immediate"


def test_esg_client_token_refresh_and_timeout_retry(monkeypatch):
    from types import SimpleNamespace

    from clause_splitter.services import esg_client as ec

    calls = {"token": 0, "post": 0}
    responses = [{"ret": "13012"}, {"ret": "13016"}, {"ret": "0"}]

    def fake_get(url, timeout=None):
        calls["token"] += 1
        return SimpleNamespace(json=lambda: {
            "ret": "0", "data": {"access_token": f"tok{calls['token']}"},
        })

    def fake_post(url, json=None, headers=None, timeout=None):
        r = responses[min(calls["post"], len(responses) - 1)]
        calls["post"] += 1
        return SimpleNamespace(json=lambda: r)

    monkeypatch.setattr(ec.requests, "get", fake_get)
    monkeypatch.setattr(ec.requests, "post", fake_post)
    sleeps: list[float] = []
    monkeypatch.setattr(ec.time, "sleep", sleeps.append)

    client = ec.EsgClient(
        auth_end_point="http://auth", open_end_point="http://open/",
        client_id="c", client_secret="s", callback_path="/cb",
    )
    result = client.post_callback({"x": 1})
    assert result == {"ret": "0"}
    assert calls == {"token": 2, "post": 3}  # 1 initial + 1 refresh after 13012
    assert sleeps == [60.0]  # one backoff for the 13016
