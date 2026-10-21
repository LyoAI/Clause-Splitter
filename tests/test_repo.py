"""Repo layer regression — CRUD / filters / job state helpers on the fake db."""

from __future__ import annotations

import pytest

from clause_splitter.db import (
    ClauseRepo,
    InternalDocRepo,
    JobRepo,
    RegulationRepo,
    ensure_indexes,
)
from tests.fakes import FakeDb


@pytest.fixture
def db() -> FakeDb:
    return FakeDb()


async def test_regulation_crud(db):
    repo = RegulationRepo(db)
    doc = await repo.create({"title": "t", "file_sha256": "abc"})
    assert doc["_id"]
    fetched = await repo.get(doc["_id"])
    assert fetched["title"] == "t"
    assert (await repo.get_by_sha256("abc"))["_id"] == doc["_id"]
    await repo.update(doc["_id"], {"title": "t2"})
    assert (await repo.get(doc["_id"]))["title"] == "t2"
    docs, total = await repo.list()
    assert total == 1 and docs[0]["_id"] == doc["_id"]
    await repo.delete(doc["_id"])
    assert await repo.get(doc["_id"]) is None


async def test_clause_replace_generations(db):
    repo = ClauseRepo(db)
    await repo.replace_for_regulation("r1", [
        {"clause_number": "第一条", "clause_text": "a"},
        {"clause_number": "第二条", "clause_text": "b"},
    ])
    rows, total = await repo.list_for_regulation("r1")
    assert total == 2 and rows[0]["_id"] == "r1:0" and rows[1]["seq"] == 1

    # A new generation replaces the old rows entirely.
    await repo.replace_for_regulation("r1", [{"clause_number": "第一条", "clause_text": "a2"}])
    rows, total = await repo.list_for_regulation("r1")
    assert total == 1 and rows[0]["clause_text"] == "a2"

    await repo.update("r1:0", {"internalization_decision": "需要内化"})
    rows, total = await repo.list_for_regulation("r1", decision="需要内化")
    assert total == 1


async def test_job_lifecycle(db):
    repo = JobRepo(db)
    job = await repo.create(job_type="split", regulation_id="r1")
    assert job["state"] == "queued"
    assert await repo.has_active_for_regulation("r1")

    await repo.update(job["_id"], {"state": "running", "progress": 0.5})
    assert await repo.has_active_for_regulation("r1")
    jobs, _ = await repo.list(regulation_id="r1", state="running")
    assert len(jobs) == 1

    await repo.update(job["_id"], {"state": "done"})
    assert not await repo.has_active_for_regulation("r1")

    # Crash recovery: queued/running → failed.
    j2 = await repo.create(job_type="chain", regulation_id="r2")
    assert await repo.mark_interrupted() == 1
    assert (await repo.get(j2["_id"]))["state"] == "failed"
    assert await repo.mark_interrupted() == 0


async def test_internal_docs_stats(db):
    repo = InternalDocRepo(db)
    await repo.create({"filename": "a.pdf", "sha256": "x", "chunk_count": 3})
    await repo.create({"filename": "b.pdf", "sha256": "y", "chunk_count": None})
    stats = await repo.stats()
    assert stats == {"doc_count": 2, "chunk_count": 3}
    assert (await repo.get_by_sha256("y"))["filename"] == "b.pdf"


async def test_ensure_indexes_noop(db):
    await ensure_indexes(db)
