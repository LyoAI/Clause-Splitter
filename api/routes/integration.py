"""Backend push route — receives regulation pushes from the system backend."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.base_fastapi import ok
from api.deps import require_api_key
from clause_splitter.services import integration_service

router = APIRouter(prefix="/integration", tags=["integration"], dependencies=[Depends(require_api_key)])


@router.post("/push")
async def push(body: dict | None = None):
    result = await integration_service.handle_push(body or {})
    return JSONResponse(status_code=202, content=ok(result, msg="chain job queued"))
