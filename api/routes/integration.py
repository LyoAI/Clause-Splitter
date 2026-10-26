"""Backend-facing routes — the external contract.

Only two endpoints touch the system backend: the push intake and the docId
result pull (callback-loss fallback). Everything else on /regulations is
internal (review page / ops).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.base_fastapi import ok
from api.deps import require_api_key
from clause_splitter.services import integration_service

router = APIRouter(prefix="/regulations", tags=["backend"], dependencies=[Depends(require_api_key)])


@router.post("/push")
async def push(body: dict | None = None):
    result = await integration_service.handle_push(body or {})
    return JSONResponse(status_code=202, content=ok(result, msg="accepted"))


@router.get("/{doc_id}/result")
async def result(doc_id: str):
    # Deliberate envelope exception: the backend parses this body exactly like
    # the ESG callback, so return the flat payload (errors still use the
    # unified error envelope with a non-2xx status).
    return await integration_service.build_result(doc_id)
