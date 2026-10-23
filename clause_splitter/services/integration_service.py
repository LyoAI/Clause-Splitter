"""Backend push integration — receive doc pushes, dispatch the chain, call back.

The system backend pushes regulation documents here (``POST /regulations/push``)
and receives the final result through the ESG callback (or pulls
``GET /regulations/{docId}/result``). Push / callback field semantics are still
being aligned with the backend team, so every assumption lives in this module —
adjusting them must not touch the pipeline.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests

from api.base_fastapi import ApiError

from clause_splitter.services import regulation_service
from clause_splitter.services.context import get_repos
from clause_splitter.services.dispatcher import dispatch_mode
from clause_splitter.services.esg_client import get_esg_client
from clause_splitter.utils import get_logger

logger = get_logger()

#: Push ``OptType`` — 0 = manual (always applicable to 平安科技), 1 = auto.
#: Mapping awaiting backend confirmation; flip here if reversed.
OPT_MANUAL = 0
DOWNLOAD_TIMEOUT_S = 60


def _title_from_url(url: str) -> str:
    """Pull the human title out of an IOBS ``attname`` query parameter."""
    attname = (parse_qs(urlparse(url).query).get("attname") or [""])[0]
    return unquote(attname).strip()


def _fetch(url: str) -> bytes:
    """http(s) via requests; anything else = a local mount path (old-version parity)."""
    if urlparse(url).scheme in ("http", "https"):
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT_S)
        resp.raise_for_status()
        return resp.content
    return Path(url).read_bytes()


def _sniff_suffix(data: bytes) -> str:
    """Suffix from magic bytes — pushed filenames/URLs lie about the format
    (the old service saw .pdf names carrying docx bodies)."""
    if data.startswith(b"%PDF-"):
        return ".pdf"
    if data.startswith(b"PK\x03\x04"):
        return ".docx"
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        return ".doc"
    try:
        data.decode("utf-8")
        return ".txt"
    except UnicodeDecodeError:
        raise ApiError(415, "unsupported download format (magic bytes unrecognized)")


def _materialize_text(text: str) -> bytes:
    """docContent (a plain str arg, not a file) → file bytes.

    Adapter for the file-driven pipeline: this is our internal representation,
    not a claim about the source format. Normalizes old-service ``<br/>``
    line breaks; the sha256 dedup still holds for repeated pushes.
    """
    return re.sub(r"<br\s*/?>", "\n", text).encode("utf-8")


async def _ensure_content(args: dict[str, Any]) -> tuple[bytes, str, str]:
    """(content, filename, title) — doclobsUrl first (old-version parity),
    docContent as the fallback when it is absent or the download fails."""
    doc_content = str(args.get("docContent") or "").strip()
    url = str(args.get("doclobsUrl") or "").strip()
    if not url and not doc_content:
        raise ApiError(400, "push args needs either docContent or doclobsUrl")

    title = Path(str(args.get("docId") or "pushed-document")).stem
    if url:
        url_title = Path(_title_from_url(url) or title).stem
        try:
            content = await asyncio.to_thread(_fetch, url)
            return content, f"{url_title}{_sniff_suffix(content)}", url_title
        except ApiError:
            raise  # e.g. 415 unknown format — retrying via docContent won't help
        except Exception as exc:  # noqa: BLE001 — degrade to docContent
            if not doc_content:
                raise ApiError(502, f"failed to download doclobsUrl: {exc}") from exc
            logger.warning("doclobsUrl download failed, using docContent: %s", exc)
    return _materialize_text(doc_content), f"{title}.txt", title


async def handle_push(body: dict[str, Any]) -> dict[str, Any]:
    """One backend push → one regulation + one chain job (manual skips stage 1).

    Accepts both ``{"args": {...}}`` and a flat field map — the wrapper is a
    draft detail pending the backend spec.
    """
    args = body.get("args") if isinstance(body.get("args"), dict) else body
    if not args.get("docId"):
        raise ApiError(400, "push args.docId is required")

    content, filename, title = await _ensure_content(args)
    repos = get_repos()
    reg, created = await regulation_service.upload(content, filename, title, repos=repos)

    manual = args.get("OptType") == OPT_MANUAL
    await repos.regulations.update(reg["_id"], {
        "push_doc_id": str(args.get("docId") or ""),
        "push_task_id": str(args.get("taskId") or ""),
        "push_uid": str(args.get("uid") or ""),
        "push_doc_type": str(args.get("docType") or ""),
        "push_opt_type": args.get("OptType"),
    })
    mode = dispatch_mode(manual)
    job = await regulation_service.start_stage(
        reg["_id"], "chain",
        payload={"skip_applicability": True} if manual else None,
        defer=mode == "scheduled",
    )
    return {
        "regulation_id": reg["_id"],
        "job_id": job["_id"],
        "task_id": str(args.get("taskId") or ""),
        "manual": manual,
        "dispatch": mode,
        "duplicate": not created,
    }


# ── result payload (shared by ESG callback and the docId pull) ──────────
#
# Contract lives in docs/callback-contract.md. Shape:
#   {code, msg, metadata, stages, data{applicability,split,internalization,analysis}}
# Every stage is ``done|failed|pending``; a stage with no results has its
# data field set to null (never an empty object).

STAGES = ("applicability", "split", "internalization", "analysis")
#: Job states still in flight — payload reports code "2" (处理中).
_PENDING_JOB_STATES = ("queued", "running", "scheduled")


async def build_result(doc_id: str) -> dict[str, Any]:
    """Result payload for one pushed docId — same shape as the callback."""
    reg = await get_repos().regulations.get_by_push_doc_id(doc_id)
    if reg is None:
        raise ApiError(404, f"no pushed regulation for docId {doc_id}")
    jobs, _total = await get_repos().jobs.list(
        regulation_id=reg["_id"], job_type="chain", limit=1,
    )
    return await _build_callback(reg, jobs[0] if jobs else None)


def _metadata(reg: dict[str, Any]) -> dict[str, Any]:
    """Push identifiers echoed back verbatim — the backend's only locators."""
    return {
        "docId": reg.get("push_doc_id", ""),
        "taskId": reg.get("push_task_id", ""),
        "uid": reg.get("push_uid", ""),
        "docType": reg.get("push_doc_type", ""),
        "optType": reg.get("push_opt_type"),
    }


def _stage_status(reg: dict[str, Any], job: dict[str, Any] | None, name: str) -> str:
    """done / failed / pending.

    ``done`` = settled: the stage ran, or an upstream branch decided it never
    will (orchestrator stamps ``skipped``; a terminal job leaves it blank).
    ``pending`` = still to come (queued / running / interrupted-and-resumable).
    """
    state = reg.get(f"{name}_state") or ""
    if state == "failed":
        return "failed"
    if state in ("done", "skipped"):
        return "done"
    job_state = (job or {}).get("state", "")
    if job_state in _PENDING_JOB_STATES or not job_state:
        return "pending"
    return "done"


def _code(job: dict[str, Any] | None) -> str:
    """1 = finished (including a clean "not applicable" stop), 0 = failed,
    2 = processing."""
    state = (job or {}).get("state", "")
    if state == "failed":
        return "0"
    if state in _PENDING_JOB_STATES or not state:
        return "2"
    return "1"


def _message(code: str, reg: dict[str, Any], job: dict[str, Any] | None) -> str:
    if code == "2":
        return "处理中"
    if code == "0":
        return str((job or {}).get("error") or "处理失败")
    applicable = reg.get("applicability_applicable")
    if applicable is False:
        return "法规不适用于本公司,未执行拆条与内化分析"
    if applicable is None:
        return "适用性待人工复核,未执行拆条与内化分析"
    return "成功"


def _chapter(row: dict[str, Any]) -> str:
    """Section path without the leaf when the leaf is the clause label."""
    name = (row.get("clause_number") or "").strip()
    parts = [p.strip() for p in (row.get("section") or "").split(" > ") if p.strip()]
    if parts and name and parts[-1] == name:
        parts.pop()
    return " > ".join(parts)


def _relation_clauses(row: dict[str, Any]) -> list[dict[str, str]]:
    """Internal clauses the episode adopted as coverage (mirrors review.py)."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for cand in row.get("candidates") or []:
        if not cand.get("adopted"):
            continue
        cid = cand.get("chunk_id") or ""
        if cid in seen:
            continue
        seen.add(cid)
        out.append({
            "internalClauseId": cid,
            "docName": cand.get("doc_name", ""),
            "clauseName": cand.get("clause_label", ""),
            "clauseContent": cand.get("full_text") or cand.get("snippet") or "",
        })
    if out:
        return out
    ids = row.get("matched_internal_clause_ids") or []
    if row.get("matched_internal_id") or ids:
        out.append({
            "internalClauseId": row.get("matched_internal_id") or str(ids[0]),
            "docName": row.get("matched_internal_document_name", ""),
            "clauseName": row.get("matched_internal_clause_number", ""),
            "clauseContent": row.get("matched_internal_source_text", ""),
        })
    return out


def _analysis_counts(reg: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, int]:
    """Stored summary when present, else derived from the clause rows."""
    summary = reg.get("analysis_summary") or {}
    if summary:
        return {
            "fullyCoveredCount": int(summary.get("fully_covered_count", 0)),
            "partiallyCoveredCount": int(summary.get("partially_covered_count", 0)),
            "missingCount": int(summary.get("missing_count", 0)),
            "internalizableCount": int(summary.get("internalizable_count", 0)),
            "noInternalizationCount": int(summary.get("no_internalization_count", 0)),
        }
    internalizable = [r for r in rows if (r.get("internalization_decision") or "") != "无需内化"]
    statuses = [r.get("status") for r in rows]
    return {
        "fullyCoveredCount": statuses.count("完全覆盖"),
        "partiallyCoveredCount": statuses.count("部分覆盖"),
        "missingCount": sum(
            1 for r in internalizable if r.get("status") == "缺失"
        ),
        "internalizableCount": len(internalizable),
        "noInternalizationCount": sum(
            1 for r in rows if r.get("internalization_decision") == "无需内化"
        ),
    }


def _result_data(
    reg: dict[str, Any], rows: list[dict[str, Any]], doc_id: str,
) -> dict[str, Any]:
    """Per-stage results; ``None`` when a stage produced nothing."""
    applicable = reg.get("applicability_applicable")
    applicability = None
    if applicable is not None or reg.get("applicability_reason"):
        applicability = {
            "applicable": applicable,
            "reason": reg.get("applicability_reason") or "",
        }

    split = None
    if rows:
        clauses = []
        for row in rows:
            order = int(row.get("seq", 0)) + 1
            name = (row.get("clause_number") or "").strip()
            clauses.append({
                "clauseId": f"{doc_id}_{order}",
                "orderNum": order,
                "chapter": _chapter(row),
                "clauseName": name,
                "clauseContent": row.get("clause_text") or "",
            })
        split = {"clauseCount": len(clauses), "clauses": clauses}

    internalization = None
    judged = [
        r for r in rows
        if r.get("clause_type") or r.get("internalization_decision")
    ]
    if judged:
        internalization = {"clauses": [
            {
                "clauseId": f"{doc_id}_{int(r.get('seq', 0)) + 1}",
                "clauseType": r.get("clause_type") or "",
                "internalize": r.get("internalization_decision") or "",
                "internalizeReason": r.get("internalization_reason") or "",
            }
            for r in judged
        ]}

    analysis = None
    analyzed = [r for r in rows if r.get("status")]
    if analyzed or reg.get("analysis_summary"):
        counts = _analysis_counts(reg, rows)
        covered = counts["fullyCoveredCount"] + counts["partiallyCoveredCount"]
        analysis = {
            # covered / internalizable — same fraction the review page shows.
            "coverageRate": f"{covered}/{counts['internalizableCount']}",
            **counts,
            "clauses": [
                {
                    "clauseId": f"{doc_id}_{int(r.get('seq', 0)) + 1}",
                    "coverageStatus": r.get("status") or "",
                    "analysis": r.get("analysis") or "",
                    "differenceAnalysis": r.get("difference_analysis") or "",
                    "coreRequirements": r.get("core_regulatory_requirements") or "",
                    "remedySuggestion": r.get("remedy_suggestion") or "",
                    "relationClauses": _relation_clauses(r),
                }
                for r in analyzed
            ],
        }

    data = {
        "applicability": applicability,
        "split": split,
        "internalization": internalization,
        "analysis": analysis,
    }
    return data if any(v is not None for v in data.values()) else None


async def _build_callback(
    reg: dict[str, Any], job: dict[str, Any] | None,
) -> dict[str, Any]:
    """Callback / pull body — see docs/callback-contract.md."""
    rows, _total = await get_repos().clauses.list_for_regulation(reg["_id"])
    rows.sort(key=lambda r: r.get("seq", 0))
    metadata = _metadata(reg)
    doc_id = metadata["docId"] or reg["_id"]
    code = _code(job)
    return {
        "code": code,
        "msg": _message(code, reg, job),
        "metadata": metadata,
        "stages": {name: _stage_status(reg, job, name) for name in STAGES},
        "data": _result_data(reg, rows, doc_id),
    }


def _post_callback(url: str, payload: dict[str, Any]) -> None:
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    logger.info(
        "backend callback delivered (task %s, http %s)",
        payload.get("metadata", {}).get("taskId"), resp.status_code,
    )


# ── result callback ─────────────────────────────────────────────────────


def register(runner) -> None:
    """Hook job completion → best-effort result callback (chain jobs only)."""
    runner.on_completion(_on_job_done)


async def _on_job_done(job: dict[str, Any]) -> None:
    from config.config_app import CONFIG_APP

    if job.get("job_type") != "chain" or not job.get("regulation_id"):
        return
    if get_esg_client() is None and not CONFIG_APP.backend_callback_url:
        return  # callback disabled (no gateway, no dev fallback)
    repos = get_repos()
    fresh = await repos.jobs.get(job["_id"])
    reg = await repos.regulations.get(job["regulation_id"])
    if reg is None or not reg.get("push_task_id"):
        return
    payload = await _build_callback(reg, fresh or job)
    try:
        await asyncio.to_thread(_deliver_callback, payload)
    except Exception as exc:  # noqa: BLE001 — callback is best-effort
        logger.error("backend callback failed for %s: %s", job["regulation_id"], exc)


def _deliver_callback(payload: dict[str, Any]) -> None:
    """ESG gateway first; plain URL as the dev fallback (tests patch this seam)."""
    from config.config_app import CONFIG_APP

    client = get_esg_client()
    if client is not None:
        result = client.post_callback(payload)
        logger.info(
            "esg callback delivered (task %s, ret %s)",
            payload.get("metadata", {}).get("taskId"), result.get("ret"),
        )
        return
    _post_callback(CONFIG_APP.backend_callback_url, payload)
