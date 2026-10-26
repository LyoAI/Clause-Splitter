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
    build_rag_service,
    data_dir,
    fake_repos,
    patch_rag,
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


async def _no_remote_parse(src, llm, service=None):
    raise RuntimeError("block parsing disabled in tests")


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
        patch_rag(monkeypatch, build_rag_service())

        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_remote_parse)
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
                "extraParams": {"source": "iobs", "batch": "2026-10"},
            }})
            assert result["manual"] is True and result["task_id"] == "task1"
            done = await _wait(repos, result["job_id"])
            assert done["state"] == "done", done["error"]

            reg = await repos.regulations.get(result["regulation_id"])
            assert reg["applicability_state"] == "done"
            assert reg["applicability_applicable"] is True
            assert "手动" in reg["applicability_reason"]
            assert reg["push_doc_id"] == "doc1" and reg["push_task_id"] == "task1"
            assert reg["push_extra_params"] == {"source": "iobs", "batch": "2026-10"}
            assert "applicability" not in llm.calls

            assert len(delivered) == 1
            payload = delivered[0]
            assert list(payload) == [
                "code", "msg", "docId", "docKeyword", "docType", "docAbstract",
                "taskId", "applicable", "applicableReason", "data",
            ]
            assert payload["code"] == "1" and payload["msg"] == "成功"
            assert payload["docId"] == "doc1"
            assert payload["taskId"] == "task1"
            assert payload["docType"] == "3"
            assert payload["applicable"] is True
            assert "手动" in payload["applicableReason"]
            assert payload["docAbstract"] == "mock 摘要:本法规规范数据安全管理与风险评估要求。"
            assert payload["docKeyword"] == "数据安全, 风险管理"

            data = payload["data"]
            assert isinstance(data, list) and len(data) == len(CLAUSE_SPECS)
            assert [c["termName"] for c in data] == [
                s["clause_number"] for s in CLAUSE_SPECS
            ]
            first = data[0]
            assert first["orderNum"] == "0"
            assert first["termId"] == "doc1-0"
            assert "金融机构应当建立数据分类分级制度" in first["termContent"]
            assert first["termKeyword"]
            assert "chapter" in first
            assert first["internalize"] == "需要内化"
            assert first["internalizeReason"]
            assert first["coverageStatus"] == "完全覆盖"
            assert first["analysis"]
            assert first["coreRequirements"]
            assert first["relationTerms"], "adopted internal clauses must be reported"
            assert first["relationTerms"][0]["internalClauseId"].startswith("数据安全管理办法")

            second = data[1]
            assert second["orderNum"] == "1"
            assert second["termId"] == "doc1-1"
            assert second["coverageStatus"] == "部分覆盖"
            assert second["differenceAnalysis"] and second["remedySuggestion"]

            # Removed legacy-bridge keys must not leak back in.
            raw = json.dumps(payload, ensure_ascii=False)
            for gone in (
                '"metadata"', '"stages"', '"relationClauses"',
                '"extraParams"', '"clauseType"',
            ):
                assert gone not in raw
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


async def test_push_extra_params_must_be_object():
    with pytest.raises(ApiError) as exc:
        await integration_service.handle_push({
            "args": {"docId": "d", "docContent": "x", "extraParams": ["bad"]},
        })
    assert exc.value.status_code == 400
    assert "extraParams" in exc.value.message


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
        patch_rag(monkeypatch, build_rag_service())

        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_remote_parse)
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
            # docId pull before admission → processing, no clauses yet
            early = await integration_service.build_result("doc3")
            assert early["code"] == "2" and early["msg"] == "处理中"
            assert early["data"] == []
            assert "stages" not in early

            assert await runner.drain_scheduled() == 1
            done = await _wait(repos, result["job_id"])
            assert done["state"] == "done", done["error"]

            payload = await integration_service.build_result("doc3")
            assert payload["docId"] == "doc3"
            assert payload["code"] == "1"
            assert len(payload["data"]) == len(CLAUSE_SPECS)
            assert payload["data"][0]["termId"] == "doc3-0"
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
            assert payload["applicable"] is False
            assert payload["applicableReason"]
            assert payload["docAbstract"]  # profile still generated on the stop branch
            assert payload["data"] == []
            assert "stages" not in payload
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
        patch_rag(monkeypatch, build_rag_service())
        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_remote_parse)


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
            assert len(payload["data"]) == len(CLAUSE_SPECS)
            assert all(c["coverageStatus"] == "" for c in payload["data"])
            assert all(c["analysis"] == "" for c in payload["data"])
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


async def test_result_route_returns_flat_payload(monkeypatch):
    """The pull endpoint is a deliberate envelope exception (backend parity)."""
    from api.routes import integration as route_mod

    async def fake_result(doc_id: str):
        return {"code": "1", "msg": "成功", "docId": doc_id, "data": []}

    monkeypatch.setattr(integration_service, "build_result", fake_result)
    body = await route_mod.result("doc1")
    assert body == {"code": "1", "msg": "成功", "docId": "doc1", "data": []}
    assert "status" not in body


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
