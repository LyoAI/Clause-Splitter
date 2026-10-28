"""Regulation use-cases — upload / stage orchestration / overrides.

Routes call these; they own preconditions (409), re-run resets and enqueue
atomicity (per-regulation lock around check + enqueue).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from api.base_fastapi import ApiError

from clause_splitter.pipeline.contexts import DERIVED_FIELDS, DOWNSTREAM, STAGES, is_internal
from clause_splitter.storage import (
    regulation_dir,
    safe_filename,
    source_dir,
)
from clause_splitter.utils import get_logger

logger = get_logger()

REGULATION_SUFFIXES = {".pdf", ".docx", ".doc", ".txt", ".md"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def upload(
    data: bytes,
    filename: str,
    title: str,
    *,
    repos=None,
) -> tuple[dict, bool]:
    """Store one regulation upload; sha256 dedupe returns the existing row."""
    repos = repos or _repos()
    suffix = Path(filename).suffix.lower()
    if suffix not in REGULATION_SUFFIXES:
        raise ApiError(415, f"unsupported file type: {suffix or '(none)'}; allowed: {sorted(REGULATION_SUFFIXES)}")

    sha = _sha256(data)
    existing = await repos.regulations.get_by_sha256(sha)
    if existing is not None:
        return existing, False

    reg_id = (await repos.regulations.create({
        "title": "",
        "source_filename": filename,
        "source_path": "",
        "file_sha256": sha,
        **{f"{s}_state": "not_run" for s in STAGES},
    }))["_id"]

    path = source_dir(reg_id) / safe_filename(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    await repos.regulations.update(reg_id, {
        "title": (title or Path(filename).stem).strip(),
        "source_path": str(path),
        **DERIVED_FIELDS["applicability"],
        **DERIVED_FIELDS["split"],
        **DERIVED_FIELDS["internalization"],
        **DERIVED_FIELDS["analysis"],
    })
    return await repos.regulations.get(reg_id), True


async def delete_regulation(reg_id: str, *, repos=None) -> None:
    repos = repos or _repos()
    if await repos.jobs.has_active_for_regulation(reg_id):
        raise ApiError(409, "a job is active for this regulation; wait or let it finish first")
    reg = await repos.regulations.get(reg_id)
    if reg is None:
        raise ApiError(404, "regulation not found")
    await repos.clauses.delete_for_regulation(reg_id)
    await repos.regulations.delete(reg_id)
    shutil.rmtree(regulation_dir(reg_id), ignore_errors=True)
    if is_internal(reg):
        await _drop_internal_mirror(reg)


async def _drop_internal_mirror(reg: dict) -> None:
    """Remove the ``internal_docs`` mirror row a 内规/附件 push created.

    Best-effort and synchronous on purpose: deleting a regulation must not fail
    because the corpus out of sync. Only push-created rows are touched — a
    ``kind="library"`` row is the ops upload and outlives the regulation.
    """
    from clause_splitter.services import library_service

    sha = reg.get("file_sha256") or ""
    if not sha:
        return
    try:
        repos = _repos()
        row = await repos.internal_docs.get_by_sha256(sha)
        if row is None or (row.get("kind") or "library") == "library":
            return
        await library_service.delete_doc(row["_id"])
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        logger.warning("删除法规时清理入库镜像失败 (%s): %s", reg.get("_id"), exc)


async def override_applicability(
    reg_id: str, applicable: bool, reason: str, *, repos=None
) -> dict:
    """Human decision after 需人工复核 — marks the stage done."""
    repos = repos or _repos()
    reg = await repos.regulations.get(reg_id)
    if reg is None:
        raise ApiError(404, "regulation not found")
    await repos.regulations.update(reg_id, {
        "applicability_applicable": bool(applicable),
        "applicability_reason": reason,
        "applicability_confidence": 1.0,
        "applicability_state": "done",
    })
    return await repos.regulations.get(reg_id)


async def start_stage(reg_id: str, job_type: str, *, force: bool = False,
                      payload: dict | None = None, defer: bool = False,
                      replace_scheduled: bool = False) -> dict:
    """Validate preconditions, reset downstream, enqueue one stage / chain.

    ``defer=True`` parks the job in ``scheduled`` (cron admission) instead of
    the live queue — used by backend pushes under cron/auto dispatch.
    ``replace_scheduled=True`` lets a re-push cancel a pending scheduled chain
    job instead of raising 409; queued/running jobs still block.
    """
    runner = _runner()
    repos = runner.repos
    async with runner.lock_for(reg_id):
        reg = await repos.regulations.get(reg_id)
        if reg is None:
            raise ApiError(404, "regulation not found")
        active = await repos.jobs.list_active_for_regulation(reg_id)
        if active:
            replaceable = (
                replace_scheduled
                and job_type == "chain"
                and all(
                    job.get("job_type") == "chain" and job.get("state") == "scheduled"
                    for job in active
                )
            )
            if not replaceable:
                raise ApiError(409, "a job is already queued/running for this regulation")
            # The cron dispatcher may admit a job between the list and the
            # conditional update; if that happens, keep the 409 and let the
            # caller retry after it finishes.
            cancelled = 0
            for job in active:
                if await repos.jobs.cancel_scheduled(job["_id"], "superseded by re-push"):
                    cancelled += 1
            if cancelled != len(active):
                raise ApiError(409, "a job is already queued/running for this regulation")
            logger.info(
                "re-push superseded %d scheduled chain job(s) for %s", cancelled, reg_id
            )
        _check_precondition(reg, job_type, force)
        await _reset(reg_id, job_type, repos)
        if defer:
            job = await repos.jobs.create(
                job_type=job_type, regulation_id=reg_id,
                payload=payload or {}, state="scheduled",
            )
            logger.info("job scheduled: %s (%s) for %s", job["_id"], job_type, reg_id)
            return job
        return await runner.enqueue(job_type, regulation_id=reg_id, payload=payload)


def _check_precondition(reg: dict, job_type: str, force: bool) -> None:
    def state(stage: str) -> str:
        return reg.get(f"{stage}_state") or "not_run"

    if job_type == "applicability":
        return
    if job_type == "split":
        # 内规/附件 never run applicability, so it is not a precondition for them.
        if not is_internal(reg):
            if state("applicability") != "done":
                raise ApiError(409, "run the applicability stage first")
            if reg.get("applicability_applicable") is not True and not force:
                raise ApiError(409, "regulation not applicable (or needs review); pass {\"force\": true} to split anyway")
    elif job_type == "internalization":
        if state("split") != "done":
            raise ApiError(409, "run the split stage first")
    elif job_type == "analysis":
        if state("internalization") != "done":
            raise ApiError(409, "run the internalization stage first")
    elif job_type != "chain":
        raise ApiError(400, f"unknown job type: {job_type}")


async def _reset(reg_id: str, job_type: str, repos) -> None:
    """Stage → pending; downstream stages → not_run with derived data cleared."""
    fields: dict = {f"{job_type}_state": "pending"}
    for stage in DOWNSTREAM.get(job_type, ()):
        fields[f"{stage}_state"] = "not_run"
        fields.update(DERIVED_FIELDS.get(stage, {}))
    await repos.regulations.update(reg_id, fields)


def _runner():
    from clause_splitter.services.context import get_runner

    return get_runner()


def _repos():
    from clause_splitter.services.context import get_repos

    return get_repos()
