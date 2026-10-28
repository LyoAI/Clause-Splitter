"""Shared API helpers — auth dependency and Mongo-doc serializers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Request

from api.base_fastapi import ApiError
from clause_splitter.pipeline.contexts import STAGES
from config.config_app import CONFIG_APP


async def require_api_key(request: Request) -> None:
    """X-API-Key required on /api/v1/clause_splitter/* when API_KEY is configured."""
    if not CONFIG_APP.api_key:
        return
    if request.headers.get("X-API-Key") != CONFIG_APP.api_key:
        raise ApiError(401, "invalid or missing X-API-Key", error_code="UNAUTHORIZED")


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


def reg_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "title": doc.get("title") or "",
        "source_filename": doc.get("source_filename") or "",
        "doc_kind": doc.get("push_doc_kind") or "external",
        "article_count": doc.get("article_count"),
        "applicability": {
            "applicable": doc.get("applicability_applicable"),
            "confidence": doc.get("applicability_confidence") or 0.0,
            "reason": doc.get("applicability_reason") or "",
            "truncated": bool(doc.get("applicability_truncated")),
        },
        "stages": {s: doc.get(f"{s}_state") or "not_run" for s in STAGES},
        "split_mode": doc.get("split_mode") or "",
        "doc_abstract": doc.get("doc_abstract") or "",
        "doc_keyword": doc.get("doc_keyword") or "",
        "analysis_summary": doc.get("analysis_summary"),
        "error": doc.get("error") or "",
        "created_at": _iso(doc.get("created_at")),
        "updated_at": _iso(doc.get("updated_at")),
    }


_CLAUSE_BASE_FIELDS = (
    "clause_number", "section", "clause_text", "clause_type", "term_keyword",
    "internalization_decision", "internalization_reason",
    "status", "analysis", "difference_analysis",
    "core_regulatory_requirements", "remedy_suggestion",
    "matched_internal_id", "matched_internal_document_name",
    "matched_internal_clause_number", "matched_internal_source_text",
    "matched_internal_clause_ids",
)


def clause_out(doc: dict[str, Any], *, include_candidates: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": doc["_id"],
        "regulation_id": doc.get("regulation_id"),
        "seq": doc.get("seq"),
        **{k: doc.get(k, "" if not isinstance(doc.get(k), list) else []) for k in _CLAUSE_BASE_FIELDS},
        "updated_at": _iso(doc.get("updated_at")),
    }
    if include_candidates:
        out["candidates"] = doc.get("candidates") or []
    return out


def job_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "job_type": doc.get("job_type"),
        "regulation_id": doc.get("regulation_id") or "",
        "state": doc.get("state"),
        "progress": doc.get("progress") or 0.0,
        "step": doc.get("step") or "",
        "error": doc.get("error") or "",
        "payload": doc.get("payload") or {},
        "created_at": _iso(doc.get("created_at")),
        "updated_at": _iso(doc.get("updated_at")),
    }


def internal_doc_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "filename": doc.get("filename"),
        "kind": doc.get("kind") or "library",
        "size_bytes": doc.get("size_bytes") or 0,
        "chunk_count": doc.get("chunk_count"),
        "remote_doc_id": doc.get("remote_doc_id") or "",
        "remote_state": doc.get("remote_state") or "",
        "remote_error": doc.get("remote_error") or "",
        "indexed_at": _iso(doc.get("indexed_at")),
        "created_at": _iso(doc.get("created_at")),
    }
