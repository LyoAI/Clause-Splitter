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
from clause_splitter.rag import RagUnavailable

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
                "optType": 0, "docContent": SOURCE_TEXT, "docType": "1",
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
            assert payload["docType"] == "1"
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
                "optType": 1, "docType": "1", "docId": "doc2",
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
                "optType": 1, "docId": "docA", "taskId": "tA",
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
                "optType": 1, "docId": "docB", "taskId": "tB",
                "doclobsUrl": "http://iobs.example/dead-link", "docContent": SOURCE_TEXT,
            }})
            reg = await repos.regulations.get(result["regulation_id"])
            assert reg["source_filename"] == "docB.txt"

            # no docContent to fall back on → hard 502
            with pytest.raises(ApiError) as exc:
                await integration_service.handle_push({"args": {
                    "optType": 1, "docId": "docC", "taskId": "tC",
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
    """Flat (unwrapped) body + auto dispatch: optType=1 parks in scheduled
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
                "optType": 1, "docContent": SOURCE_TEXT, "docType": "1",
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
            assert payload["applicable"] is True  # external chain still judges
            assert len(payload["data"]) == len(CLAUSE_SPECS)
            assert payload["data"][0]["termId"] == "doc3-0"
        finally:
            await runner.stop()
            services.reset()


async def test_re_push_supersedes_scheduled_chain_and_reruns(tmp_path, monkeypatch):
    """Re-pushing the same bytes replaces a pending scheduled chain job.

    Changing docId/taskId does not create a new regulation (sha256 dedupe);
    the old scheduled job must be cancelled so the fresh job can run.
    """
    with data_dir(tmp_path):
        monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "auto")
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: NoApplicabilityLLM())
        services.init(repos, runner)
        integration_service.register(runner)
        patch_rag(monkeypatch, build_rag_service())
        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_remote_parse)

        await runner.start()
        try:
            first = await integration_service.handle_push({"args": {
                "optType": 1, "docContent": SOURCE_TEXT, "docId": "doc5", "taskId": "t5",
            }})
            assert first["dispatch"] == "scheduled"
            old = await repos.jobs.get(first["job_id"])
            assert old["state"] == "scheduled"

            second = await integration_service.handle_push({"args": {
                "optType": 0, "docContent": SOURCE_TEXT, "docId": "doc5b", "taskId": "t5b",
            }})
            assert second["job_id"] != first["job_id"]
            assert second["dispatch"] == "immediate"
            assert second["duplicate"] is True  # same sha256 → same regulation

            old = await repos.jobs.get(first["job_id"])
            assert old["state"] == "failed"
            assert "superseded" in old["error"]

            done = await _wait(repos, second["job_id"])
            assert done["state"] == "done", done["error"]
            reg = await repos.regulations.get(second["regulation_id"])
            assert reg["push_doc_id"] == "doc5b"
            assert reg["push_task_id"] == "t5b"
            assert reg["push_opt_type"] == 0
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
                "optType": 1, "docContent": SOURCE_TEXT, "docId": "docX", "taskId": "tX",
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
                "optType": 1, "docContent": SOURCE_TEXT, "docId": "docY", "taskId": "tY",
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
                "optType": 0, "docContent": SOURCE_TEXT, "docId": "doc4", "taskId": "t4",
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


# ── docType classification (routing not implemented; classification logged) ──


@pytest.mark.parametrize("doc_type,kind", [
    ("1", "external"),
    ("2", "internal"),
    ("3", "attach"),
    ("", "external"),        # missing → external
    ("9", "external"),       # unknown → external
])
def test_classify_doc_type(doc_type, kind):
    assert integration_service.classify_doc_type(doc_type)[0] == kind


async def test_push_records_doc_type_and_kind(tmp_path):
    with data_dir(tmp_path):
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: PipelineFakeLLM())
        services.init(repos, runner)
        try:
            result = await integration_service.handle_push({"args": {
                "optType": 1, "docId": "doc-internal", "taskId": "t",
                "docType": "2", "docContent": SOURCE_TEXT,
            }})
            # The push response shape is part of the backend contract — unchanged.
            assert list(result) == [
                "regulation_id", "job_id", "task_id", "manual", "dispatch", "duplicate",
            ]
            reg = await repos.regulations.get(result["regulation_id"])
            assert reg["push_doc_type"] == "2"
            assert reg["push_doc_kind"] == "internal"
        finally:
            await runner.stop()
            services.reset()


# ── internal / attachment pushes (docType 2 and 3) ──────────────────────


async def test_internal_push_ingests_and_splits_only(tmp_path, monkeypatch):
    """docType=2: corpus upload + split; no applicability / internalization / analysis."""
    with data_dir(tmp_path):
        monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "immediate")
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: NoApplicabilityLLM())
        services.init(repos, runner)
        integration_service.register(runner)
        service = patch_rag(monkeypatch, build_rag_service())
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
                "optType": 0, "docContent": SOURCE_TEXT, "docType": "2",
                "docId": "intl1", "taskId": "ti1",
            }})
            done = await _wait(repos, result["job_id"])
            assert done["state"] == "done", done["error"]

            reg = await repos.regulations.get(result["regulation_id"])
            assert reg["push_doc_kind"] == "internal"
            assert reg["applicability_state"] == "skipped"
            assert reg["internalization_state"] == "skipped"
            assert reg["analysis_state"] == "skipped"
            assert reg["split_state"] == "done"
            assert reg["applicability_applicable"] is None  # never stamped True
            assert reg["article_count"] == len(CLAUSE_SPECS)

            # Corpus upload with the internal corpus label.
            assert service.ingests[0][0] == reg["source_filename"]
            assert service.ingest_labels[0] == ["internal-regulation"]

            # Mirror row lands in the library view too.
            mirror = await repos.internal_docs.get_by_sha256(reg["file_sha256"])
            assert mirror is not None and mirror["kind"] == "internal"
            assert mirror["remote_state"] == "indexed"

            # Callback still fires, with the external-only fields empty.
            assert len(delivered) == 1
            payload = delivered[0]
            assert payload["code"] == "1" and payload["msg"] == "成功"
            assert payload["applicable"] is None
            assert payload["applicableReason"] == ""
            assert len(payload["data"]) == len(CLAUSE_SPECS)
            first = payload["data"][0]
            assert first["termName"] == CLAUSE_SPECS[0]["clause_number"]
            assert first["termContent"]
            assert first["internalize"] == "" and first["internalizeReason"] == ""
            assert first["coverageStatus"] == "" and first["analysis"] == ""
            assert first["differenceAnalysis"] == ""
            assert first["coreRequirements"] == "" and first["remedySuggestion"] == ""
            assert first["relationTerms"] == []
        finally:
            await runner.stop()
            services.reset()
            object.__setattr__(config_app_mod.CONFIG_APP, "backend_callback_url", "")


async def test_attach_push_uses_the_attach_label(tmp_path, monkeypatch):
    """docType=3 behaves exactly like 内规, only the corpus label differs."""
    with data_dir(tmp_path):
        monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "immediate")
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: NoApplicabilityLLM())
        services.init(repos, runner)
        service = patch_rag(monkeypatch, build_rag_service())
        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_remote_parse)
        await runner.start()
        try:
            result = await integration_service.handle_push({"args": {
                "optType": 0, "docContent": SOURCE_TEXT, "docType": "3",
                "docId": "att1", "taskId": "ta1",
            }})
            done = await _wait(repos, result["job_id"])
            assert done["state"] == "done", done["error"]
            assert service.ingest_labels[0] == ["attach-regulation"]
            reg = await repos.regulations.get(result["regulation_id"])
            assert reg["push_doc_kind"] == "attach"
            mirror = await repos.internal_docs.get_by_sha256(reg["file_sha256"])
            assert mirror["kind"] == "attach"
        finally:
            await runner.stop()
            services.reset()


async def test_internal_push_ingest_failure_fails_the_job(tmp_path, monkeypatch):
    """Corpus upload is the point of the push: its failure fails the job, but the
    split results stay retrievable through /result."""
    with data_dir(tmp_path):
        monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "immediate")
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: NoApplicabilityLLM())
        services.init(repos, runner)
        service = patch_rag(monkeypatch, build_rag_service())
        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_remote_parse)
        service.ingest_error = RagUnavailable("zeus 502 bad gateway")
        await runner.start()
        try:
            result = await integration_service.handle_push({"args": {
                "optType": 0, "docContent": SOURCE_TEXT, "docType": "2",
                "docId": "intlBad", "taskId": "tib",
            }})
            done = await _wait(repos, result["job_id"])
            assert done["state"] == "failed"
            assert "内规入库失败" in done["error"]

            payload = await integration_service.build_result("intlBad")
            assert payload["code"] == "0"
            assert "入库失败" in payload["msg"]
            assert len(payload["data"]) == len(CLAUSE_SPECS)

            reg = await repos.regulations.get(result["regulation_id"])
            mirror = await repos.internal_docs.get_by_sha256(reg["file_sha256"])
            assert mirror["remote_state"] == "failed"
            assert "zeus 502" in mirror["remote_error"]
        finally:
            await runner.stop()
            services.reset()


async def test_internal_callback_can_be_disabled(tmp_path, monkeypatch):
    """PUSH_INTERNAL_CALLBACK=0: internal pushes stop calling back, external keeps going."""
    with data_dir(tmp_path):
        monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "immediate")
        monkeypatch.setattr(cs_config, "PUSH_INTERNAL_CALLBACK", False)
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: NoApplicabilityLLM())
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
            internal = await integration_service.handle_push({"args": {
                "optType": 0, "docContent": SOURCE_TEXT, "docType": "2",
                "docId": "quiet1", "taskId": "tq",
            }})
            assert (await _wait(repos, internal["job_id"]))["state"] == "done"
            assert delivered == []  # disabled for internal/attach

            external = await integration_service.handle_push({"args": {
                "optType": 0, "docContent": SOURCE_TEXT + "补充条款\n", "docType": "1",
                "docId": "loud1", "taskId": "tl",
            }})
            assert (await _wait(repos, external["job_id"]))["state"] == "done"
            assert len(delivered) == 1 and delivered[0]["docId"] == "loud1"
        finally:
            await runner.stop()
            services.reset()
            object.__setattr__(config_app_mod.CONFIG_APP, "backend_callback_url", "")


async def test_deleting_an_internal_regulation_removes_the_mirror_row(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        monkeypatch.setattr(cs_config, "CHAIN_DISPATCH", "immediate")
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: NoApplicabilityLLM())
        services.init(repos, runner)
        patch_rag(monkeypatch, build_rag_service())
        monkeypatch.setattr(split_stage_mod, "clauses_from_file", _no_remote_parse)
        await runner.start()
        try:
            result = await integration_service.handle_push({"args": {
                "optType": 0, "docContent": SOURCE_TEXT, "docType": "2",
                "docId": "intlDel", "taskId": "td",
            }})
            assert (await _wait(repos, result["job_id"]))["state"] == "done"
            reg = await repos.regulations.get(result["regulation_id"])
            assert await repos.internal_docs.get_by_sha256(reg["file_sha256"]) is not None

            from clause_splitter.services import regulation_service

            await regulation_service.delete_regulation(result["regulation_id"])
            assert await repos.internal_docs.get_by_sha256(reg["file_sha256"]) is None
        finally:
            await runner.stop()
            services.reset()


async def test_split_precondition_is_skipped_for_internal(tmp_path):
    """内规/附件 never run applicability, so split must not demand it."""
    from clause_splitter.services import regulation_service

    with data_dir(tmp_path):
        repos = fake_repos()
        runner = JobRunner(repos, workers=1, llm_factory=lambda: NoApplicabilityLLM())
        services.init(repos, runner)
        try:
            reg, _ = await regulation_service.upload(
                SOURCE_TEXT.encode("utf-8"), "内规.txt", "内规", repos=repos,
            )
            await repos.regulations.update(reg["_id"], {"push_doc_kind": "internal"})
            job = await regulation_service.start_stage(reg["_id"], "split")
            assert job["job_type"] == "split"

            # An external regulation still hits the 409.
            reg2, _ = await regulation_service.upload(
                (SOURCE_TEXT + "另一版\n").encode("utf-8"), "外规.txt", "外规", repos=repos,
            )
            with pytest.raises(ApiError) as exc:
                await regulation_service.start_stage(reg2["_id"], "split")
            assert exc.value.status_code == 409
        finally:
            await runner.stop()
            services.reset()


def test_local_path_title_comes_from_the_filename(tmp_path, monkeypatch):
    """api_lab --path sends a bare path, not an IOBS url with attname."""
    src = tmp_path / "网络数据安全风险评估办法.docx"
    src.write_bytes(b"PK\x03\x04docx-bytes")
    assert integration_service._title_from_url(str(src)) == "网络数据安全风险评估办法"
