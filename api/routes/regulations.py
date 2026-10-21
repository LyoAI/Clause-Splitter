"""Regulation routes — upload, stage kicks, clauses, artifacts."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from api.base_fastapi import ApiError, ok
from api.deps import clause_out, job_out, reg_out, require_api_key
from clause_splitter.services import regulation_service
from clause_splitter.storage import (
    ARTIFACTS,
    artifact_path,
    episodes_dir,
)
from config.config_app import CONFIG_APP

router = APIRouter(prefix="/regulations", tags=["regulations"], dependencies=[Depends(require_api_key)])

_EPISODE_NAME_RE = re.compile(r"^[\w\-.#]{1,200}\.json$")


def _repos():
    from clause_splitter.services.context import get_repos

    return get_repos()


async def _get_reg(reg_id: str) -> dict:
    reg = await _repos().regulations.get(reg_id)
    if reg is None:
        raise ApiError(404, "regulation not found")
    return reg


@router.post("")
async def upload_regulation(
    file: UploadFile = File(...),
    title: str = Form(""),
):
    data = await file.read()
    if not data:
        raise ApiError(400, "empty file")
    if len(data) > CONFIG_APP.max_upload_bytes:
        raise ApiError(413, f"file exceeds {CONFIG_APP.max_upload_mb} MB limit")
    doc, created = await regulation_service.upload(data, file.filename or "", title)
    body = ok({**reg_out(doc), "duplicate": not created}, msg="created" if created else "duplicate")
    return JSONResponse(status_code=201 if created else 200, content=body)


@router.get("")
async def list_regulations(limit: int = 20, offset: int = 0):
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    docs, total = await _repos().regulations.list(limit=limit, offset=offset)
    return ok({"items": [reg_out(d) for d in docs], "total": total})


@router.get("/{reg_id}")
async def get_regulation(reg_id: str):
    reg = await _get_reg(reg_id)
    jobs, _ = await _repos().jobs.list(regulation_id=reg_id, limit=1)
    out = reg_out(reg)
    out["latest_job"] = job_out(jobs[0]) if jobs else None
    return ok(out)


@router.delete("/{reg_id}", status_code=204)
async def delete_regulation(reg_id: str):
    await regulation_service.delete_regulation(reg_id)
    return Response(status_code=204)


async def _kick(reg_id: str, job_type: str, force: bool = False) -> JSONResponse:
    job = await regulation_service.start_stage(reg_id, job_type, force=force)
    return JSONResponse(status_code=202, content=ok(job_out(job), msg=f"{job_type} job queued"))


@router.post("/{reg_id}/applicability")
async def start_applicability(reg_id: str):
    await _get_reg(reg_id)
    return await _kick(reg_id, "applicability")


@router.patch("/{reg_id}/applicability")
async def override_applicability(reg_id: str, body: dict):
    applicable = body.get("applicable")
    if not isinstance(applicable, bool):
        raise ApiError(400, "body must be {\"applicable\": bool, \"reason\": str}")
    reg = await regulation_service.override_applicability(reg_id, applicable, str(body.get("reason") or ""))
    return ok(reg_out(reg), msg="applicability overridden")


@router.post("/{reg_id}/split")
async def start_split(reg_id: str, body: dict | None = None):
    await _get_reg(reg_id)
    return await _kick(reg_id, "split", force=bool((body or {}).get("force")))


@router.post("/{reg_id}/internalization")
async def start_internalization(reg_id: str):
    await _get_reg(reg_id)
    return await _kick(reg_id, "internalization")


@router.post("/{reg_id}/analysis")
async def start_analysis(reg_id: str):
    await _get_reg(reg_id)
    return await _kick(reg_id, "analysis")


@router.post("/{reg_id}/run")
async def run_chain(reg_id: str):
    await _get_reg(reg_id)
    return await _kick(reg_id, "chain")


@router.get("/{reg_id}/clauses")
async def list_clauses(
    reg_id: str,
    decision: str = "",
    status: str = "",
    include: str = "",
    limit: int = 50,
    offset: int = 0,
):
    await _get_reg(reg_id)
    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)
    rows, total = await _repos().clauses.list_for_regulation(
        reg_id, decision=decision, status=status, limit=limit, offset=offset
    )
    with_candidates = include == "candidates"
    return ok({
        "items": [clause_out(r, include_candidates=with_candidates) for r in rows],
        "total": total,
    })


@router.get("/{reg_id}/clauses/{clause_id}")
async def get_clause(reg_id: str, clause_id: str):
    await _get_reg(reg_id)
    row = await _repos().clauses.get(clause_id)
    if row is None or row.get("regulation_id") != reg_id:
        raise ApiError(404, "clause not found")
    return ok(clause_out(row, include_candidates=True))


@router.get("/{reg_id}/jobs")
async def regulation_jobs(reg_id: str, limit: int = 20, offset: int = 0):
    await _get_reg(reg_id)
    limit = min(max(limit, 1), 100)
    jobs, total = await _repos().jobs.list(regulation_id=reg_id, limit=limit, offset=max(offset, 0))
    return ok({"items": [job_out(j) for j in jobs], "total": total})


@router.get("/{reg_id}/artifacts")
async def list_artifacts(reg_id: str):
    await _get_reg(reg_id)
    items = []
    for name in ARTIFACTS:
        path = artifact_path(reg_id, name)
        if path is None or not path.is_file():
            continue
        stat = path.stat()
        items.append({
            "name": name,
            "url": f"/api/v1/regulations/{reg_id}/artifacts/{name}",
            "size": stat.st_size,
            "created_at": stat.st_mtime,
        })
    episodes = episodes_dir(reg_id)
    if episodes.is_dir():
        for f in sorted(episodes.glob("*.json")):
            items.append({
                "name": f"episodes/{f.name}",
                "url": f"/api/v1/regulations/{reg_id}/artifacts/episodes/{f.name}",
                "size": f.stat().st_size,
                "created_at": f.stat().st_mtime,
            })
    return ok({"items": items})


@router.get("/{reg_id}/artifacts/{name}")
async def download_artifact(reg_id: str, name: str):
    await _get_reg(reg_id)
    path = artifact_path(reg_id, name)
    if path is None or not path.is_file():
        raise ApiError(404, "artifact not found")
    return FileResponse(path, filename=path.name)


@router.get("/{reg_id}/artifacts/episodes/{filename}")
async def download_episode(reg_id: str, filename: str):
    await _get_reg(reg_id)
    if not _EPISODE_NAME_RE.match(filename):
        raise ApiError(400, "invalid episode filename")
    path = episodes_dir(reg_id) / filename
    if not path.is_file():
        raise ApiError(404, "episode not found")
    return FileResponse(path, filename=filename)
