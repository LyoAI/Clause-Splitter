"""Job routes — cross-regulation job queries."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.base_fastapi import ApiError, ok
from api.deps import job_out, require_api_key

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])


def _repos():
    from clause_splitter.services.context import get_repos

    return get_repos()


@router.get("")
async def list_jobs(state: str = "", job_type: str = "", limit: int = 50, offset: int = 0):
    jobs, total = await _repos().jobs.list(
        state=state, job_type=job_type, limit=min(max(limit, 1), 200), offset=max(offset, 0)
    )
    return ok({"items": [job_out(j) for j in jobs], "total": total})


@router.get("/{job_id}")
async def get_job(job_id: str):
    job = await _repos().jobs.get(job_id)
    if job is None:
        raise ApiError(404, "job not found")
    return ok(job_out(job))
