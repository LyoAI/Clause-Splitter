"""Mongo repositories — thin query layer, one class per collection.

Documents are plain dicts with string ``_id``. Services / pipeline / runner
take repo instances as arguments (built by :func:`default_repos`), so tests
inject fakes instead of touching MongoDB. All writes stamp ``updated_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from common.mongo_async import get_mongo_db

#: States that block a new job on the same regulation.
JOB_ACTIVE_STATES = ("queued", "running", "scheduled")
#: States that cannot survive a restart — ``scheduled`` is future work, it persists.
JOB_INTERRUPTIBLE_STATES = ("queued", "running")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> datetime:
    return datetime.now(timezone.utc)


class RegulationRepo:
    def __init__(self, db: Any | None = None) -> None:
        self._col = (get_mongo_db() if db is None else db).regulations

    async def create(self, doc: dict[str, Any]) -> dict[str, Any]:
        doc = {
            **doc,
            "_id": doc.get("_id") or new_id(),
            "created_at": now(),
            "updated_at": now(),
        }
        await self._col.insert_one(doc)
        return doc

    async def get(self, reg_id: str) -> dict[str, Any] | None:
        return await self._col.find_one({"_id": reg_id})

    async def get_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        return await self._col.find_one({"file_sha256": sha256}, sort=[("created_at", -1)])

    async def get_by_push_doc_id(self, doc_id: str) -> dict[str, Any] | None:
        """Latest regulation pushed under a backend docId (newest re-push wins)."""
        return await self._col.find_one({"push_doc_id": doc_id}, sort=[("created_at", -1)])

    async def list(self, *, limit: int = 20, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        total = await self._col.count_documents({})
        cursor = self._col.find({}, sort=[("created_at", -1)]).skip(offset).limit(limit)
        return await cursor.to_list(length=limit), total

    async def update(self, reg_id: str, fields: dict[str, Any]) -> None:
        await self._col.update_one({"_id": reg_id}, {"$set": {**fields, "updated_at": now()}})

    async def delete(self, reg_id: str) -> None:
        await self._col.delete_one({"_id": reg_id})


class ClauseRepo:
    def __init__(self, db: Any | None = None) -> None:
        self._col = (get_mongo_db() if db is None else db).clauses

    async def replace_for_regulation(self, reg_id: str, clauses: list[dict[str, Any]]) -> int:
        """One generation per split run: drop old rows, insert the new batch."""
        await self._col.delete_many({"regulation_id": reg_id})
        docs = [
            {
                **c,
                "_id": f"{reg_id}:{seq}",
                "regulation_id": reg_id,
                "seq": seq,
                "created_at": now(),
                "updated_at": now(),
            }
            for seq, c in enumerate(clauses)
        ]
        if docs:
            await self._col.insert_many(docs)
        return len(docs)

    async def list_for_regulation(
        self,
        reg_id: str,
        *,
        decision: str = "",
        status: str = "",
        limit: int = 10**6,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        query: dict[str, Any] = {"regulation_id": reg_id}
        if decision:
            query["internalization_decision"] = decision
        if status:
            query["status"] = status
        total = await self._col.count_documents(query)
        cursor = self._col.find(query, sort=[("seq", 1)]).skip(offset).limit(limit)
        return await cursor.to_list(length=limit), total

    async def get(self, clause_id: str) -> dict[str, Any] | None:
        return await self._col.find_one({"_id": clause_id})

    async def update(self, clause_id: str, fields: dict[str, Any]) -> None:
        await self._col.update_one({"_id": clause_id}, {"$set": {**fields, "updated_at": now()}})

    async def delete_for_regulation(self, reg_id: str) -> None:
        await self._col.delete_many({"regulation_id": reg_id})


class JobRepo:
    def __init__(self, db: Any | None = None) -> None:
        self._col = (get_mongo_db() if db is None else db).jobs

    async def create(
        self,
        *,
        job_type: str,
        regulation_id: str = "",
        payload: dict[str, Any] | None = None,
        state: str = "queued",
    ) -> dict[str, Any]:
        doc = {
            "_id": new_id(),
            "job_type": job_type,
            "regulation_id": regulation_id,
            "state": state,
            "progress": 0.0,
            "step": "",
            "payload": payload or {},
            "error": "",
            "created_at": now(),
            "updated_at": now(),
        }
        await self._col.insert_one(doc)
        return doc

    async def get(self, job_id: str) -> dict[str, Any] | None:
        return await self._col.find_one({"_id": job_id})

    async def update(self, job_id: str, fields: dict[str, Any]) -> None:
        await self._col.update_one({"_id": job_id}, {"$set": {**fields, "updated_at": now()}})

    async def list(
        self,
        *,
        state: str = "",
        job_type: str = "",
        regulation_id: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        query: dict[str, Any] = {}
        if state:
            query["state"] = state
        if job_type:
            query["job_type"] = job_type
        if regulation_id:
            query["regulation_id"] = regulation_id
        total = await self._col.count_documents(query)
        cursor = self._col.find(query, sort=[("created_at", -1)]).skip(offset).limit(limit)
        return await cursor.to_list(length=limit), total

    async def has_active_for_regulation(self, reg_id: str) -> bool:
        """Any queued/running/scheduled job on this regulation (409 upstream)."""
        return await self._col.count_documents(
            {"regulation_id": reg_id, "state": {"$in": list(JOB_ACTIVE_STATES)}}
        ) > 0

    async def list_active_for_regulation(self, reg_id: str) -> list[dict[str, Any]]:
        """All queued/running/scheduled jobs on this regulation."""
        cursor = self._col.find(
            {"regulation_id": reg_id, "state": {"$in": list(JOB_ACTIVE_STATES)}},
            sort=[("created_at", 1)],
        )
        return await cursor.to_list(length=100)

    async def cancel_scheduled(self, job_id: str, reason: str) -> bool:
        """Cancel one scheduled job; false if the cron dispatcher already admitted it."""
        result = await self._col.update_one(
            {"_id": job_id, "state": "scheduled"},
            {"$set": {"state": "failed", "error": reason, "updated_at": now()}},
        )
        return result.modified_count > 0

    async def mark_interrupted(self) -> int:
        """Startup crash recovery: queued/running jobs can't survive a restart."""
        result = await self._col.update_many(
            {"state": {"$in": list(JOB_INTERRUPTIBLE_STATES)}},
            {"$set": {"state": "failed", "error": "interrupted by restart", "updated_at": now()}},
        )
        return result.modified_count

    async def claim_scheduled(self, limit: int = 100) -> list[dict[str, Any]]:
        """Flip scheduled jobs to queued (FIFO) and return the admitted rows.

        Single-process dispatch (``--workers 1``); a plain list+update is
        enough — the worker re-checks state before running.
        """
        rows, _total = await self.list(state="scheduled", limit=limit)
        rows.reverse()  # list() is newest-first; admit oldest first
        for job in rows:
            await self._col.update_one(
                {"_id": job["_id"], "state": "scheduled"},
                {"$set": {"state": "queued", "updated_at": now()}},
            )
            job["state"] = "queued"
        return rows


class InternalDocRepo:
    def __init__(self, db: Any | None = None) -> None:
        self._col = (get_mongo_db() if db is None else db).internal_docs

    async def create(self, doc: dict[str, Any]) -> dict[str, Any]:
        doc = {
            **doc,
            "_id": doc.get("_id") or new_id(),
            "created_at": now(),
            "updated_at": now(),
        }
        await self._col.insert_one(doc)
        return doc

    async def get(self, doc_id: str) -> dict[str, Any] | None:
        return await self._col.find_one({"_id": doc_id})

    async def get_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        return await self._col.find_one({"sha256": sha256})

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        total = await self._col.count_documents({})
        cursor = self._col.find({}, sort=[("created_at", -1)]).skip(offset).limit(limit)
        return await cursor.to_list(length=limit), total

    async def update(self, doc_id: str, fields: dict[str, Any]) -> None:
        await self._col.update_one({"_id": doc_id}, {"$set": {**fields, "updated_at": now()}})

    async def delete(self, doc_id: str) -> None:
        await self._col.delete_one({"_id": doc_id})

    async def stats(self) -> dict[str, int]:
        """{doc_count, chunk_count} for GET /library/index."""
        rows = await self._col.find({}, {"chunk_count": 1}).to_list(length=None)
        return {
            "doc_count": len(rows),
            "chunk_count": sum(int(r.get("chunk_count") or 0) for r in rows),
        }


async def ensure_indexes(db: Any | None = None) -> None:
    """Idempotent index creation; called once at startup."""
    _db = get_mongo_db() if db is None else db
    await _db.regulations.create_index("file_sha256")
    await _db.clauses.create_index([("regulation_id", 1), ("seq", 1)])
    await _db.jobs.create_index([("regulation_id", 1), ("state", 1)])
    await _db.jobs.create_index([("state", 1), ("created_at", -1)])
    await _db.internal_docs.create_index("sha256", unique=True)


def default_repos(db: Any | None = None) -> tuple[RegulationRepo, ClauseRepo, JobRepo, InternalDocRepo]:
    """Build the repo bundle from one shared db handle."""
    _db = get_mongo_db() if db is None else db
    return RegulationRepo(_db), ClauseRepo(_db), JobRepo(_db), InternalDocRepo(_db)
