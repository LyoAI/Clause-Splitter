"""In-process async job runner — queue + bounded workers, state in Mongo.

Jobs are persisted rows (db.JobRepo); the queue holds ids only. One
queued/running job per regulation is enforced upstream (409); the runner owns
job state transitions and progress writes (throttled). Stage stage-states are
managed by the stage functions / chain for chain jobs, by the runner for
single-stage jobs. Startup recovery marks orphaned queued/running jobs failed.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from clause_splitter.utils import get_logger
from clause_splitter.orchestrator import run_chain
from clause_splitter.pipeline import applicability, analysis, internalize, split_stage
from clause_splitter.pipeline.contexts import Repos, StageContext, StageError

logger = get_logger()


#: Built-in regulation-stage handlers (job_type → async fn(ctx) -> dict).
STAGE_JOBS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "applicability": applicability.run,
    "split": split_stage.run,
    "internalization": internalize.run,
    "analysis": analysis.run,
    "chain": run_chain,
}


class JobRunner:
    """Dispatches queued jobs onto ``workers`` asyncio tasks."""

    def __init__(
        self,
        repos: Repos,
        *,
        workers: int = 2,
        llm_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.repos = repos
        self.workers = max(1, workers)
        self.llm_factory = llm_factory or self._default_llm
        self._handlers: dict[str, Callable[[StageContext], Awaitable[dict[str, Any]]]] = {}
        self._handlers.update(STAGE_JOBS)  # type: ignore[arg-type]
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._reg_locks: dict[str, asyncio.Lock] = {}
        self._on_done: list[Callable[[dict[str, Any]], Awaitable[None]]] = []
        self._llm: Any = None

    # ── wiring ────────────────────────────────────────────────────────────

    @staticmethod
    def _default_llm() -> Any:
        from clause_splitter.config import get_settings
        from clause_splitter.llm import get_llm

        return get_llm(get_settings().default_model)

    def register(self, job_type: str, handler: Callable[[StageContext], Awaitable[dict[str, Any]]]) -> None:
        """Register an extra job type (e.g. ``index_build``)."""
        self._handlers[job_type] = handler

    def on_completion(self, fn: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Best-effort async hook fired when a job reaches a terminal state."""
        self._on_done.append(fn)

    async def _notify_done(self, job: dict[str, Any]) -> None:
        for fn in self._on_done:
            try:
                await fn(job)
            except Exception as exc:  # noqa: BLE001 — hooks must not kill the worker
                logger.error("completion hook failed: %s", exc)

    def lock_for(self, reg_id: str) -> asyncio.Lock:
        """Per-regulation enqueue lock (check-then-enqueue atomicity)."""
        return self._reg_locks.setdefault(reg_id, asyncio.Lock())

    @property
    def llm(self) -> Any:
        if self._llm is None:
            self._llm = self.llm_factory()
        return self._llm

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        interrupted = await self.repos.jobs.mark_interrupted()
        if interrupted:
            logger.warning("recovery: %d interrupted job(s) marked failed", interrupted)
        self._tasks = [
            asyncio.create_task(self._worker(), name=f"job-worker-{i}")
            for i in range(self.workers)
        ]
        logger.info("job runner started with %d worker(s)", self.workers)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("job runner stopped")

    # ── enqueue ───────────────────────────────────────────────────────────

    async def enqueue(
        self,
        job_type: str,
        *,
        regulation_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job = await self.repos.jobs.create(
            job_type=job_type, regulation_id=regulation_id, payload=payload or {}
        )
        await self._queue.put(job["_id"])
        logger.info("job queued: %s (%s) for %s", job["_id"], job_type, regulation_id or "-")
        return job

    async def drain_scheduled(self, limit: int = 100) -> int:
        """Admit scheduled jobs onto the queue (cron tick / manual trigger)."""
        admitted = await self.repos.jobs.claim_scheduled(limit)
        for job in admitted:
            await self._queue.put(job["_id"])
        if admitted:
            logger.info("dispatcher admitted %d scheduled job(s)", len(admitted))
        return len(admitted)

    # ── workers ───────────────────────────────────────────────────────────

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                job = await self.repos.jobs.get(job_id)
                if job is None or job.get("state") != "queued":
                    continue
                await self._dispatch(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a job crash never kills the worker
                logger.error("job %s crashed: %s", job_id, e)
                try:
                    await self.repos.jobs.update(job_id, {"state": "failed", "error": str(e)})
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._queue.task_done()

    async def _dispatch(self, job: dict[str, Any]) -> None:
        job_type = job["job_type"]
        handler = self._handlers.get(job_type)
        if handler is None:
            await self.repos.jobs.update(job["_id"], {"state": "failed", "error": f"unknown job type: {job_type}"})
            return

        regulation: dict[str, Any] = {"_id": job["regulation_id"], "title": ""}
        # index_build uses the "__library__" sentinel; it has no regulation row.
        if job["regulation_id"] and job_type != "index_build":
            doc = await self.repos.regulations.get(job["regulation_id"])
            if doc is None:
                await self.repos.jobs.update(job["_id"], {"state": "failed", "error": "regulation not found"})
                return
            regulation = doc

        from clause_splitter.rag import get_rag_service

        ctx = StageContext(
            regulation=regulation,
            llm=self.llm,
            repos=self.repos,
            payload=job.get("payload") or {},
            rag=get_rag_service(),
        )

        try:
            # An analysis-only job has nothing to do without retrieval; chains
            # check after stage 1 (a not-applicable regulation still completes).
            if job_type == "analysis" and not ctx.rag.available:
                raise StageError("RAG 服务未配置 —— 请设置 RAG_API_BASE 后重启服务")
            await self._execute(
                job, handler, ctx, single_stage=job_type not in ("analysis", "chain")
            )
        except Exception as e:  # noqa: BLE001 — job-level failure
            logger.error("job %s (%s) failed: %s", job["_id"], job_type, e)
            await self.repos.jobs.update(
                job["_id"], {"state": "failed", "error": str(e) or type(e).__name__}
            )
        finally:
            await self._notify_done(job)

    async def _execute(
        self,
        job: dict[str, Any],
        handler: Callable[[StageContext], Awaitable[dict[str, Any]]],
        ctx: StageContext,
        *,
        single_stage: bool,
    ) -> None:
        await self.repos.jobs.update(job["_id"], {"state": "running", "step": "started"})
        last = {"progress": -1.0, "step": ""}
        progress_task: asyncio.Task | None = None

        def on_progress(progress: float, step: str) -> None:
            nonlocal progress_task
            progress = round(min(max(progress, 0.0), 1.0), 3)
            if progress == last["progress"] and step == last["step"]:
                return
            last["progress"], last["step"] = progress, step
            progress_task = asyncio.create_task(
                self.repos.jobs.update(job["_id"], {"progress": progress, "step": step})
            )

        ctx.progress = on_progress
        stage_field = f"{job['job_type']}_state"

        if single_stage:
            await self.repos.regulations.update(ctx.reg_id, {stage_field: "running"})
        try:
            out = await handler(ctx) or {}
        except Exception:
            if single_stage:
                await self.repos.regulations.update(ctx.reg_id, {stage_field: "failed"})
            raise
        finally:
            # Drain the in-flight progress write so it can't land after the
            # terminal update below and clobber the final progress/state.
            if progress_task is not None:
                try:
                    await progress_task
                except Exception:  # noqa: BLE001 — progress write is best-effort
                    pass
        if single_stage:
            await self.repos.regulations.update(ctx.reg_id, {stage_field: "done", **out})
        await self.repos.jobs.update(job["_id"], {"state": "done", "progress": 1.0, "step": "done"})
        logger.info("job %s (%s) done", job["_id"], job["job_type"])
