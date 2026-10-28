"""Internal-library use-cases over the external RAG service."""

from __future__ import annotations

import pytest

from api.base_fastapi import ApiError
from clause_splitter.jobs import JobRunner
from clause_splitter.pipeline.contexts import StageContext
from clause_splitter.rag import RagUnavailable
from clause_splitter.services import context as services
from clause_splitter.services import library_service
from tests.fakes import FakeRagService, data_dir, fake_repos, patch_rag


async def _init(tmp_path, monkeypatch):
    repos = fake_repos()
    runner = JobRunner(repos, workers=1, llm_factory=lambda: object())
    services.init(repos, runner)
    service = patch_rag(monkeypatch, FakeRagService())
    return repos, runner, service


async def test_upload_forwards_to_the_service_and_mirrors(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        repos, runner, service = await _init(tmp_path, monkeypatch)
        try:
            doc, created = await library_service.upload(b"# rules", "制度.md")
            assert created and doc["remote_state"] == "indexed"
            assert doc["remote_doc_id"] == doc["_id"]
            assert doc["chunk_count"] == 1
            assert doc["indexed_at"]
            assert service.ingests[0][0] == "制度.md"
            assert service.ingest_labels[0] == ["internal-regulation"]
            assert (tmp_path / "internal" / "docs" / "制度.md").exists()

            again, created2 = await library_service.upload(b"# rules", "制度.md")
            assert not created2 and again["_id"] == doc["_id"]
            assert len(service.ingests) == 1  # duplicate never re-ingests
        finally:
            await runner.stop()
            services.reset()


async def test_upload_survives_an_unavailable_service(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        repos, runner, service = await _init(tmp_path, monkeypatch)
        service.ingest_error = RagUnavailable("down")
        try:
            doc, created = await library_service.upload(b"x", "制度.md")
            assert created and doc["remote_state"] == "pending"
            assert doc["chunk_count"] is None
        finally:
            await runner.stop()
            services.reset()


async def test_index_build_reingests_every_row(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        repos, runner, service = await _init(tmp_path, monkeypatch)
        try:
            await library_service.upload(b"a", "甲.md")
            await library_service.upload(b"b", "乙.md")
            service.ingests.clear()
            service.ingest_error = RagUnavailable("down")  # recovery run
            ctx = StageContext(
                regulation={"_id": "__library__", "title": ""},
                llm=None, repos=repos, rag=service,
            )
            library_service.register_index_job(runner)
            handler = runner._handlers["index_build"]
            with pytest.raises(ApiError):
                await handler(ctx)
            assert len(service.ingests) == 0

            service.ingest_error = None
            out = await handler(ctx)
            assert len(service.ingests) == 2
            assert out == {"doc_count": 3, "chunk_count": 9, "version": "fake-v1"}
        finally:
            await runner.stop()
            services.reset()


async def test_delete_removes_remote_and_local(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        repos, runner, service = await _init(tmp_path, monkeypatch)
        try:
            doc, _ = await library_service.upload(b"x", "制度.md")
            path = tmp_path / "internal" / "docs" / "制度.md"
            service.chunks.append(type(service.chunks[0])("x#0", doc["_id"], 0, "x"))
            await library_service.delete_doc(doc["_id"])
            assert service.deletes == [doc["_id"]]
            assert await repos.internal_docs.get(doc["_id"]) is None
            assert not path.exists()
            with pytest.raises(ApiError) as exc:
                await library_service.delete_doc(doc["_id"])
            assert exc.value.status_code == 404
        finally:
            await runner.stop()
            services.reset()


async def test_index_status_merges_remote_stats(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        repos, runner, service = await _init(tmp_path, monkeypatch)
        try:
            await library_service.upload(b"x", "制度.md")
            status = await library_service.index_status()
            assert status["doc_count"] == 1
            assert status["rag"]["available"] is True
            assert status["rag"]["version"] == "fake-v1"
            assert status["building"] is False
        finally:
            await runner.stop()
            services.reset()


async def test_upload_rejects_unsupported_suffix(tmp_path, monkeypatch):
    with data_dir(tmp_path):
        repos, runner, service = await _init(tmp_path, monkeypatch)
        try:
            with pytest.raises(ApiError) as exc:
                await library_service.upload(b"x", "archive.zip")
            assert exc.value.status_code == 415
        finally:
            await runner.stop()
            services.reset()
