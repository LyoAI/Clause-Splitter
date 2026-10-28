"""Internal-library use-cases — document management + corpus sync.

The corpus lives in the external RAG service; this module keeps the local file
(the source of truth for re-ingestion) plus a Mongo mirror row (remote doc id,
chunk count, state). Uploads forward immediately; ``POST /library/index``
re-runs ingestion for every row (idempotent upsert by doc id), which is also the
recovery path when the service was down at upload time.

Backend pushes of 内规/附件 (docType 2/3) reuse ``upload`` too, with
``kind="internal"`` / ``"attach"`` and ``strict=True``: the mirror row and the
corpus label must be right, so a failed ingest fails the push job instead of
leaving a silent ``pending`` row.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from api.base_fastapi import ApiError

from clause_splitter.config import ZEUS_LABEL_ATTACH, ZEUS_LABEL_INTERNAL
from clause_splitter.db import now
from clause_splitter.rag import RagError, get_rag_service
from clause_splitter.services.context import get_repos, get_runner
from clause_splitter.storage import internal_docs_dir, safe_filename
from clause_splitter.utils import get_logger

logger = get_logger()

#: ``.txt`` is here because push ``docContent`` payloads are materialized as
#: text, and those pushes are ingested too.
LIBRARY_SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg", ".md", ".markdown", ".txt",
}

#: Kind → corpus label. ``library`` is the ops upload; the other two come from
#: backend pushes and only differ in which corpus they land in.
KIND_LABELS = {
    "library": ZEUS_LABEL_INTERNAL,
    "internal": ZEUS_LABEL_INTERNAL,
    "attach": ZEUS_LABEL_ATTACH,
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _sync_row(repos, service, row: dict) -> dict:
    """Ingest one local file into the corpus and refresh the Mongo mirror."""
    path = Path(row.get("path") or "")
    if not path.exists():
        logger.warning("library row %s has no local file: %s", row["_id"], path)
        await repos.internal_docs.update(row["_id"], {"remote_state": "missing_file"})
        return row

    kind = row.get("kind") or "library"
    label = KIND_LABELS.get(kind, ZEUS_LABEL_INTERNAL)
    info = await asyncio.to_thread(
        service.ingest, path, row.get("filename") or path.name,
        row.get("remote_doc_id") or row["_id"],
        labels=[label],
    )
    fields = {
        "remote_doc_id": info.doc_id or row.get("remote_doc_id") or row["_id"],
        "chunk_count": info.chunk_count,
        "remote_state": info.state,
        "remote_error": "",
        "indexed_at": now(),
    }
    await repos.internal_docs.update(row["_id"], fields)
    logger.success(
        "internal doc synced: %s (kind=%s, label=%s, remote=%s, state=%s, chunks=%s)",
        row.get("filename"), kind, label,
        fields["remote_doc_id"], fields["remote_state"], fields["chunk_count"],
    )
    return {**row, **fields}


async def upload(
    data: bytes, filename: str, *, kind: str = "library", strict: bool = False,
) -> tuple[dict, bool]:
    """Store one internal document; sha256 dedupe returns the existing row.

    ``kind`` picks the corpus label (``library`` / ``internal`` / ``attach``).
    Re-uploading the same bytes under a different kind re-ingests them, so a
    document can move corpora instead of silently keeping the old label.
    ``strict=True`` (backend push) re-raises an ingest failure after recording
    it on the row — a pushed 内规/附件 must not look indexed when it is not.
    """
    repos = get_repos()
    suffix = Path(filename).suffix.lower()
    if suffix not in LIBRARY_SUFFIXES:
        raise ApiError(415, f"unsupported file type: {suffix or '(none)'}; allowed: {sorted(LIBRARY_SUFFIXES)}")

    sha = _sha256(data)
    existing = await repos.internal_docs.get_by_sha256(sha)
    if existing is not None:
        moved = (existing.get("kind") or "library") != kind
        logger.info(
            "internal doc dedupe hit: %s → %s%s",
            filename, existing.get("_id"),
            f" (kind {existing.get('kind') or 'library'} → {kind})" if moved else "",
        )
        if moved:
            # Same bytes, different corpus: the row follows the new kind and is
            # re-ingested (the old label stays on the remote copy until it is
            # re-submitted, which the sync below does).
            await repos.internal_docs.update(existing["_id"], {"kind": kind})
            existing = await repos.internal_docs.get(existing["_id"]) or existing
        # A push must not accept a stale mirror row as "already ingested".
        stale = strict and (existing.get("remote_state") or "") != "indexed"
        if not moved and not stale:
            return existing, False
        service = get_rag_service()
        try:
            return await _sync_row(repos, service, existing), False
        except RagError as exc:
            await _record_failure(repos, existing["_id"], exc, strict=strict, filename=filename)
            return await repos.internal_docs.get(existing["_id"]), False

    doc = await repos.internal_docs.create({
        "filename": filename,
        "path": "",
        "sha256": sha,
        "size_bytes": len(data),
        "kind": kind,
        "chunk_count": None,
        "remote_doc_id": "",
        "remote_state": "pending",
        "remote_error": "",
        "indexed_at": None,
    })
    path = internal_docs_dir() / safe_filename(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    await repos.internal_docs.update(doc["_id"], {"path": str(path)})
    doc["path"] = str(path)
    logger.info(
        "internal doc uploaded: %s (%d bytes, kind=%s) → %s",
        filename, len(data), kind, doc["_id"],
    )

    # Keep the upload even when the service is down — a later index build syncs.
    # A backend push is stricter: the ingest is the point of the push.
    service = get_rag_service()
    try:
        doc = await _sync_row(repos, service, doc)
    except RagError as exc:
        await _record_failure(repos, doc["_id"], exc, strict=strict, filename=filename)
    return await repos.internal_docs.get(doc["_id"]), True


async def _record_failure(repos, doc_id: str, exc: RagError, *, strict: bool, filename: str) -> None:
    """Log the failed ingest; a strict (push) caller also fails and marks the row."""
    await repos.internal_docs.update(doc_id, {"remote_error": str(exc)})
    if strict:
        await repos.internal_docs.update(doc_id, {"remote_state": "failed"})
        raise exc
    logger.warning("remote ingest deferred for %s: %s", filename, exc)


async def delete_doc(doc_id: str) -> None:
    runner = get_runner()
    repos = runner.repos
    async with runner.lock_for("__library__"):
        doc = await repos.internal_docs.get(doc_id)
        if doc is None:
            raise ApiError(404, "internal document not found")
        if await _index_building(repos):
            raise ApiError(409, "index build in progress; try again later")

        remote_id = doc.get("remote_doc_id") or doc_id
        try:
            removed = await asyncio.to_thread(get_rag_service().delete, remote_id)
            if not removed:
                logger.info("remote delete unsupported; remove %s from the RAG corpus separately", remote_id)
        except RagError as exc:
            logger.warning("remote delete failed for %s: %s", remote_id, exc)

        await repos.internal_docs.delete(doc_id)
        path = Path(doc.get("path") or "")
        if path.exists():
            path.unlink(missing_ok=True)
        logger.info("internal doc deleted: %s (%s)", doc_id, doc.get("filename"))


async def _index_building(repos) -> bool:
    return bool(await repos.jobs.has_active_for_regulation("__library__"))


def register_index_job(runner) -> None:
    """Attach the ``index_build`` handler (writer lock inside)."""

    async def index_build(ctx) -> dict[str, Any]:
        repos = ctx.repos
        rows, _ = await repos.internal_docs.list(limit=10**6)
        if not rows:
            raise ApiError(409, "internal library is empty — upload documents first")

        service = ctx.rag or get_rag_service()
        if not service.available:
            raise ApiError(409, "RAG 服务未配置（RAG_API_BASE / provider 凭据），无法入库")

        logger.info("index build start: %d doc(s) → %s", len(rows), service.name)
        failed: list[str] = []
        for i, row in enumerate(rows):
            try:
                await _sync_row(repos, service, row)
            except RagError as exc:
                failed.append(row.get("filename") or row["_id"])
                logger.warning("ingest failed for %s: %s", row.get("filename"), exc)
            ctx.progress((i + 1) / len(rows), f"入库 {i + 1}/{len(rows)}")

        stats = await asyncio.to_thread(service.stats)
        if failed:
            raise ApiError(
                502, f"{len(failed)} / {len(rows)} 份文档入库失败: {', '.join(failed[:5])}"
            )
        logger.success(
            "index build done: %d docs / %d chunks (version=%s)",
            stats.doc_count, stats.chunk_count, stats.version or "-",
        )
        return {
            "doc_count": stats.doc_count,
            "chunk_count": stats.chunk_count,
            "version": stats.version,
        }

    runner.register("index_build", index_build)


async def enqueue_index_build() -> dict:
    """POST /library/index — one build at a time."""
    runner = get_runner()
    repos = runner.repos
    async with runner.lock_for("__library__"):
        if await _index_building(repos):
            raise ApiError(409, "an index build is already queued/running")
        return await runner.enqueue("index_build", regulation_id="__library__")


async def index_status() -> dict[str, Any]:
    repos = get_repos()
    mirror = await repos.internal_docs.stats()
    rows, _ = await repos.internal_docs.list(limit=1)
    built_at = rows[0].get("indexed_at") if rows else None

    service = get_rag_service()
    remote: dict[str, Any] = {"available": service.available}
    if service.available:
        try:
            stats = await asyncio.to_thread(service.stats)
            remote.update(
                doc_count=stats.doc_count, chunk_count=stats.chunk_count, version=stats.version
            )
        except RagError as exc:
            remote["error"] = str(exc)
    return {
        **mirror,
        "built_at": built_at,
        "building": await _index_building(repos),
        "rag": remote,
    }

