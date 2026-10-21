"""JobRunner tests — execution, failure isolation, recovery, RWLock."""

from __future__ import annotations

import asyncio

from clause_splitter.jobs import AsyncRWLock, JobRunner
from clause_splitter.pipeline.contexts import StageError
from tests.fakes import fake_repos


async def _wait(repos, job_id: str, timeout_s: float = 10.0) -> dict:
    for _ in range(int(timeout_s / 0.05)):
        job = await repos.jobs.get(job_id)
        if job["state"] in ("done", "failed"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish in time")


async def test_runner_executes_handler_and_marks_stage_done():
    repos = fake_repos()
    runner = JobRunner(repos, workers=1, llm_factory=lambda: object())

    async def handler(ctx):
        ctx.progress(0.5, "half")
        return {"custom_field": 7}

    runner.register("custom", handler)
    reg = await repos.regulations.create({"title": "t"})
    # start() BEFORE enqueue — startup recovery fails any job already queued.
    await runner.start()
    job = await runner.enqueue("custom", regulation_id=reg["_id"])
    try:
        done = await _wait(repos, job["_id"])
        assert done["state"] == "done" and done["progress"] == 1.0
        doc = await repos.regulations.get(reg["_id"])
        assert doc["custom_state"] == "done" and doc["custom_field"] == 7
    finally:
        await runner.stop()


async def test_runner_failure_marks_job_and_stage_failed():
    repos = fake_repos()
    runner = JobRunner(repos, workers=1, llm_factory=lambda: object())

    async def handler(ctx):
        raise StageError("boom")

    runner.register("custom", handler)
    reg = await repos.regulations.create({"title": "t"})
    await runner.start()
    job = await runner.enqueue("custom", regulation_id=reg["_id"])
    try:
        failed = await _wait(repos, job["_id"])
        assert failed["state"] == "failed" and "boom" in failed["error"]
        doc = await repos.regulations.get(reg["_id"])
        assert doc["custom_state"] == "failed"
        # The worker survives: a follow-up job still runs.

        async def ok_handler(ctx):
            return {}

        runner.register("custom", ok_handler)
        job2 = await runner.enqueue("custom", regulation_id=reg["_id"])
        assert (await _wait(repos, job2["_id"]))["state"] == "done"
    finally:
        await runner.stop()


async def test_runner_recovers_interrupted_jobs():
    repos = fake_repos()
    stale = await repos.jobs.create(job_type="chain", regulation_id="r1")
    running = await repos.jobs.create(job_type="split", regulation_id="r1")
    await repos.jobs.update(running["_id"], {"state": "running"})

    runner = JobRunner(repos, workers=1, llm_factory=lambda: object())
    await runner.start()
    try:
        assert (await repos.jobs.get(stale["_id"]))["state"] == "failed"
        assert (await repos.jobs.get(running["_id"]))["state"] == "failed"
    finally:
        await runner.stop()


async def test_unknown_job_type_fails_job():
    repos = fake_repos()
    runner = JobRunner(repos, workers=1, llm_factory=lambda: object())
    reg = await repos.regulations.create({"title": "t"})
    await runner.start()
    job = await runner.enqueue("nope", regulation_id=reg["_id"])
    try:
        failed = await _wait(repos, job["_id"])
        assert "unknown job type" in failed["error"]
    finally:
        await runner.stop()


async def test_rwlock_writer_waits_for_readers():
    lock = AsyncRWLock()
    order: list[str] = []

    async def writer():
        async with lock.write():
            order.append("write")

    async def reader():
        async with lock.read():
            order.append("read2")

    async with lock.read():
        order.append("read1")
        w = asyncio.create_task(writer())
        await asyncio.sleep(0.05)
        assert order == ["read1"]  # writer blocked while a reader holds the lock
    await w
    assert order == ["read1", "write"]

    # Concurrent readers are allowed.
    async with lock.read():
        async with lock.read():
            pass


async def test_rwlock_reader_waits_for_writer():
    lock = AsyncRWLock()
    order: list[str] = []

    async def reader():
        async with lock.read():
            order.append("read")

    async with lock.write():
        r = asyncio.create_task(reader())
        await asyncio.sleep(0.05)
        assert order == []  # reader blocked while the writer holds the lock
    await r
    assert order == ["read"]
