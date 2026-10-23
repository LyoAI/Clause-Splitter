"""Backend push integration — manual/auto push, download fallback, callback."""

from __future__ import annotations

import asyncio
import json

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


async def _no_mineru(src, llm):
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
        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_mineru)
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
            assert payload["code"] == "1" and payload["msg"] == "成功"
            assert payload["metadata"] == {
                "docId": "doc1", "taskId": "task1", "uid": "u1",
                "docType": "3", "optType": 0,
            }
            assert payload["stages"] == {
                "applicability": "done", "split": "done",
                "internalization": "done", "analysis": "done",
            }
            data = payload["data"]
            assert data["applicability"]["applicable"] is True
            split = data["split"]
            assert split["clauseCount"] == len(CLAUSE_SPECS)
            assert [c["clauseName"] for c in split["clauses"]] == [
                s["clause_number"] for s in CLAUSE_SPECS
            ]
            assert split["clauses"][0]["clauseId"] == "doc1_1"
            assert split["clauses"][0]["orderNum"] == 1
            assert "term" not in json.dumps(payload)  # clause naming only

            intl = data["internalization"]["clauses"]
            assert len(intl) == len(CLAUSE_SPECS)
            assert intl[0]["internalize"] == "需要内化"

            analysis = data["analysis"]
            covered = analysis["fullyCoveredCount"] + analysis["partiallyCoveredCount"]
            assert analysis["coverageRate"] == f"{covered}/{analysis['internalizableCount']}"
            assert {c["coverageStatus"] for c in analysis["clauses"]} >= {"完全覆盖", "部分覆盖"}
            first = analysis["clauses"][0]
            assert first["relationClauses"], "adopted internal clauses must be reported"
            assert first["relationClauses"][0]["internalClauseId"].startswith("数据安全管理办法")
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
            assert reg["source_filename"].endswith(".txt")  # utf-8 sniffed, not attname's lie
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


async def test_url_wins_over_doccontent(tmp_path, monkeypatch):
    """Both present → the URL's bytes are the document (old-version parity),
    with the suffix sniffed from magic bytes."""
    with data_dir(tmp_path):
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: PipelineFakeLLM())
        services.init(repos, runner)
        try:
            monkeypatch.setattr(integration_service, "_fetch", lambda url: b"PK\x03\x04docx-bytes")
            result = await integration_service.handle_push({"args": {
                "OptType": 1, "docId": "docA", "taskId": "tA",
                "doclobsUrl": "http://iobs.example/a?attname=%E5%88%B6%E5%BA%A6.pdf",
                "docContent": "第一条　URL 与内容并存时不应使用我。",
            }})
            reg = await repos.regulations.get(result["regulation_id"])
            assert reg["source_filename"].endswith(".docx")  # PK magic, not the .pdf attname
        finally:
            await runner.stop()
            services.reset()


async def test_bad_url_falls_back_to_doccontent(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: PipelineFakeLLM())
        services.init(repos, runner)
        try:
            def boom(url):
                raise OSError("connection refused")

            monkeypatch.setattr(integration_service, "_fetch", boom)
            result = await integration_service.handle_push({"args": {
                "OptType": 1, "docId": "docB", "taskId": "tB",
                "doclobsUrl": "http://iobs.example/dead-link", "docContent": SOURCE_TEXT,
            }})
            reg = await repos.regulations.get(result["regulation_id"])
            assert reg["source_filename"] == "docB.txt"

            # no docContent to fall back on → hard 502
            with pytest.raises(ApiError) as exc:
                await integration_service.handle_push({"args": {
                    "OptType": 1, "docId": "docC", "taskId": "tC",
                    "doclobsUrl": "http://iobs.example/dead-link",
                }})
            assert exc.value.status_code == 502
        finally:
            await runner.stop()
            services.reset()


def test_sniff_suffix_and_local_path_fetch(tmp_path):
    import pytest as _pytest

    sniff = integration_service._sniff_suffix
    assert sniff(b"%PDF-1.7...") == ".pdf"
    assert sniff(b"PK\x03\x04rest") == ".docx"
    assert sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") == ".doc"
    assert sniff("第一条　纯文本。".encode("utf-8")) == ".txt"
    with _pytest.raises(ApiError) as exc:
        sniff(b"\xff\xfe\x80binary")  # invalid UTF-8, no known magic
    assert exc.value.status_code == 415

    # non-http scheme = local mount path, read as bytes
    local = tmp_path / "mounted.pdf"
    local.write_bytes(b"%PDF-local")
    assert integration_service._fetch(str(local)) == b"%PDF-local"

    # docContent str → file bytes adapter (old-service <br/> line breaks)
    assert integration_service._materialize_text("第一条　甲。<br/>第二条　乙。<br>") == \
        "第一条　甲。\n第二条　乙。\n".encode("utf-8")


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
        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_mineru)
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
            # docId pull before admission → processing, every stage pending
            early = await integration_service.build_result("doc3")
            assert early["code"] == "2" and early["msg"] == "处理中"
            assert set(early["stages"].values()) == {"pending"}
            assert early["data"] is None

            assert await runner.drain_scheduled() == 1
            done = await _wait(repos, result["job_id"])
            assert done["state"] == "done", done["error"]

            payload = await integration_service.build_result("doc3")
            assert payload["metadata"]["docId"] == "doc3"
            assert payload["code"] == "1"
            assert len(payload["data"]["split"]["clauses"]) == len(CLAUSE_SPECS)
        finally:
            await runner.stop()
            services.reset()


async def test_not_applicable_stops_chain_with_null_stages(tmp_path, monkeypatch):
    """Not applicable → clean stop: code 1, downstream stages done with null data."""
    with data_dir(tmp_path):
        monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "immediate")
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: PipelineFakeLLM(applicable=False))
        services.init(repos, runner)
        store = build_store()

        async def pinned_store(ctx):
            return store

        monkeypatch.setattr(runner, "_pinned_store", pinned_store)
        try:
            await runner.start()
            result = await integration_service.handle_push({"args": {
                "OptType": 1, "docContent": SOURCE_TEXT, "docId": "docX", "taskId": "tX",
            }})
            done = await _wait(repos, result["job_id"])
            assert done["state"] == "done", done["error"]

            payload = await integration_service.build_result("docX")
            assert payload["code"] == "1"
            assert "不适用于本公司" in payload["msg"]
            assert payload["stages"] == {
                "applicability": "done", "split": "done",
                "internalization": "done", "analysis": "done",
            }
            assert payload["data"]["applicability"]["applicable"] is False
            assert payload["data"]["split"] is None
            assert payload["data"]["internalization"] is None
            assert payload["data"]["analysis"] is None
        finally:
            await runner.stop()
            services.reset()


async def test_failed_stage_delivers_partial_results(tmp_path, monkeypatch):
    """Internalization fails → code 0, split results still delivered."""
    with data_dir(tmp_path):
        monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "immediate")
        repos = fake_repos()
        llm = PipelineFakeLLM()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: llm)
        services.init(repos, runner)
        store = build_store()
        patch_registry(monkeypatch, store)
        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_mineru)

        async def pinned_store(ctx):
            return store

        monkeypatch.setattr(runner, "_pinned_store", pinned_store)

        async def boom(ctx):
            raise RuntimeError("internalization boom")

        import clause_splitter.pipeline.internalize as internalize_mod

        monkeypatch.setattr(internalize_mod, "run", boom)
        try:
            await runner.start()
            result = await integration_service.handle_push({"args": {
                "OptType": 1, "docContent": SOURCE_TEXT, "docId": "docY", "taskId": "tY",
            }})
            done = await _wait(repos, result["job_id"])
            assert done["state"] == "failed"

            payload = await integration_service.build_result("docY")
            assert payload["code"] == "0"
            assert "internalization boom" in payload["msg"]
            assert payload["stages"] == {
                "applicability": "done", "split": "done",
                "internalization": "failed", "analysis": "done",
            }
            assert len(payload["data"]["split"]["clauses"]) == len(CLAUSE_SPECS)
            assert payload["data"]["internalization"] is None
            assert payload["data"]["analysis"] is None
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
