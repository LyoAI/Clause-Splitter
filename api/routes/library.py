"""Internal-library routes — documents + index."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse, Response

from api.base_fastapi import ApiError, ok
from api.deps import internal_doc_out, require_api_key
from clause_splitter.services import library_service
from clause_splitter.services.context import get_repos

router = APIRouter(prefix="/library", tags=["library"], dependencies=[Depends(require_api_key)])


@router.post("/documents")
async def upload_document(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise ApiError(400, "empty file")
    doc, created = await library_service.upload(data, file.filename or "")
    body = ok(internal_doc_out(doc), msg="created" if created else "duplicate")
    return JSONResponse(status_code=201 if created else 200, content=body)


@router.get("/documents")
async def list_documents(limit: int = 100, offset: int = 0):
    docs, total = await get_repos().internal_docs.list(
        limit=min(max(limit, 1), 500), offset=max(offset, 0)
    )
    return ok({"items": [internal_doc_out(d) for d in docs], "total": total})


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: str):
    await library_service.delete_doc(doc_id)
    return Response(status_code=204)


@router.post("/index")
async def build_index(body: dict | None = None):
    job = await library_service.enqueue_index_build(bool((body or {}).get("rebuild")))
    return JSONResponse(status_code=202, content=ok({"job_id": job["_id"]}, msg="index build queued"))


@router.get("/index")
async def index_status():
    return ok(await library_service.index_status())
