"""Internal-library use-cases — document management + index building.

Uploads land in ``data/internal/docs`` (no indexing); ``POST /library/index``
enqueues an ``index_build`` job that runs ``prepare_docs`` (incremental by
sha256) under the runner's writer lock.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from api.base_fastapi import ApiError

from clause_splitter.storage import internal_docs_dir, internal_index_root, safe_filename
from clause_splitter.utils import get_logger

logger = get_logger()

LIBRARY_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".txt", ".md"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _repos():
    from clause_splitter.services.context import get_repos

    return get_repos()


def _runner():
    from clause_splitter.services.context import get_runner

    return get_runner()


async def upload(data: bytes, filename: str) -> tuple[dict, bool]:
    """Store one internal document; sha256 dedupe returns the existing row."""
    repos = _repos()
    suffix = Path(filename).suffix.lower()
    if suffix not in LIBRARY_SUFFIXES:
        raise ApiError(415, f"unsupported file type: {suffix or '(none)'}; allowed: {sorted(LIBRARY_SUFFIXES)}")

    sha = _sha256(data)
    existing = await repos.internal_docs.get_by_sha256(sha)
    if existing is not None:
        return existing, False

    doc = await repos.internal_docs.create({
        "filename": filename,
        "path": "",
        "sha256": sha,
        "size_bytes": len(data),
        "chunk_count": None,
        "indexed_at": None,
    })
    path = internal_docs_dir() / safe_filename(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    await repos.internal_docs.update(doc["_id"], {"path": str(path)})
    return await repos.internal_docs.get(doc["_id"]), True


async def delete_doc(doc_id: str) -> None:
    runner = _runner()
    repos = runner.repos
    async with runner.lock_for("__library__"):
        doc = await repos.internal_docs.get(doc_id)
        if doc is None:
            raise ApiError(404, "internal document not found")
        if await _index_building(repos):
            raise ApiError(409, "index build in progress; try again later")
        await repos.internal_docs.delete(doc_id)
        path = Path(doc.get("path") or "")
        if path.exists():
            path.unlink(missing_ok=True)
        # A deleted document changes the corpus — drop the derived index state.
        logger.info("internal doc deleted: %s (%s)", doc_id, doc.get("filename"))


async def _index_building(repos) -> bool:
    jobs, _ = await repos.jobs.list(job_type="index_build", state="running", limit=1)
    return bool(jobs) or bool(await repos.jobs.has_active_for_regulation("__library__"))


def register_index_job(runner) -> None:
    """Attach the ``index_build`` handler (writer lock inside)."""

    async def index_build(ctx) -> dict[str, Any]:
        from clause_splitter.compare.index import prepare_docs

        rebuild = bool(ctx.payload.get("rebuild"))
        docs_dir = internal_docs_dir()
        if not docs_dir.exists() or not any(docs_dir.iterdir()):
            raise ApiError(409, "internal library is empty — upload documents first")

        store = await asyncio.to_thread(
            prepare_docs,
            docs_dir,
            name="internal",
            index_root=internal_index_root(),
            rebuild=rebuild,
        )

        # Per-document chunk counts + indexed_at (best-effort by filename stem).
        repos = ctx.repos
        indexed_at = _now()
        for row in (await repos.internal_docs.list(limit=10**6))[0]:
            stem = Path(row["filename"]).stem
            chunks = len(store.docs.get(stem, []))
            await repos.internal_docs.update(row["_id"], {
                "chunk_count": chunks,
                "indexed_at": indexed_at,
            })

        logger.info(
            "index build done: %d docs / %d chunks (rebuild=%s)",
            len(store.docs), len(store.all_chunks()), rebuild,
        )
        return {"doc_count": len(store.docs), "chunk_count": len(store.all_chunks())}

    runner.register("index_build", index_build)


async def enqueue_index_build(rebuild: bool = False) -> dict:
    """POST /library/index — one build at a time."""
    runner = _runner()
    repos = runner.repos
    async with runner.lock_for("__library__"):
        if await _index_building(repos):
            raise ApiError(409, "an index build is already queued/running")
        return await runner.enqueue("index_build", regulation_id="__library__", payload={"rebuild": rebuild})


async def index_status() -> dict[str, Any]:
    repos = _repos()
    stats = await repos.internal_docs.stats()
    rows, _ = await repos.internal_docs.list(limit=1)
    built_at = rows[0].get("indexed_at") if rows else None
    building = await _index_building(repos)
    return {
        **stats,
        "built_at": built_at,
        "building": building,
    }


def _now():
    from clause_splitter.db.repo import now

    return now()
