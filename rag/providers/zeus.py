"""Zeus knowledge-base provider — search only.

All Zeus wire details live here: header auth (``x-app-id`` / ``x-app-secret`` /
``x-index`` / ``x-request-id``), the ``{status,msg,data}`` envelope and
``standard_search``. Ingestion and parsing are not part of this client; the
internal corpus is loaded by the ingestion side.

Zeus labels separate corpora inside one index. ``search`` takes an optional
per-call ``labels`` filter (``None`` / empty = whole knowledge base); the
document id of a hit is its ``org_data_id`` — the id the caller recorded at
ingestion (the backend docId).
"""

from __future__ import annotations

import threading
import uuid
from typing import Any
from urllib.parse import quote

import requests

from utils import get_logger

from ..base import RagService
from ..errors import (
    RagAuthError,
    RagError,
    RagNotConfigured,
    RagNotFound,
    RagResponseError,
    RagUnavailable,
)
from ..types import Hit

logger = get_logger()

_SEARCH = "/api/v1/uns/zeus-search/{app_id}/server/standard_search"
# How long a thread may wait for a concurrency slot before giving up.
_QUEUE_TIMEOUT_S = 120.0


class ZeusProvider(RagService):
    name = "zeus"

    def __init__(
        self,
        base_url: str = "",
        app_id: str = "",
        app_secret: str = "",
        index: str = "",
        *,
        timeout_s: float = 30.0,
        kb_limit: int = 20,
        max_k: int = 50,
        recall_min_score: float = 0.1,
        rerank_min_score: float = 0.0,
        max_concurrency: int = 2,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.index = index
        self.timeout_s = timeout_s
        self.kb_limit = max(1, kb_limit)
        self.max_k = max(1, max_k)
        self.recall_min_score = recall_min_score
        self.rerank_min_score = rerank_min_score
        self.max_concurrency = max(1, max_concurrency)
        self._session = requests.Session()
        self._sem = threading.BoundedSemaphore(self.max_concurrency)
        logger.info(
            "zeus provider ready: base=%s index=%s concurrency=%d available=%s",
            self.base_url or "-", self.index or "-", self.max_concurrency, self.available,
        )

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.app_id and self.app_secret and self.index)

    def search(self, query: str, k: int, labels: list[str] | None = None) -> list[Hit]:
        k = max(1, min(int(k or 0), self.max_k))
        body: dict[str, Any] = {
            "session_id": str(uuid.uuid4()),
            "query": query,
            "kb": [{
                "alias": self.index,
                "filter": {
                    "data_type": ["doc"],
                    "text_labels": [str(lb) for lb in (labels or [])],
                    "min_score": self.recall_min_score,
                },
                "limit": max(k, self.kb_limit),
            }],
            "limit": k,
        }
        if self.rerank_min_score > 0:
            body["min_score"] = self.rerank_min_score
        try:
            rows = self._request(
                "POST", _SEARCH.format(app_id=quote(self.app_id, safe="")), json=body
            ) or []
        except RagNotFound as exc:
            # 404 here means search was never enabled for this app: a deployment
            # problem, not "no hits".
            raise RagNotConfigured(
                "Zeus 检索未配置(404) —— 请联系 RAG 团队为该应用开通检索"
            ) from exc

        raw = rows if isinstance(rows, list) else []
        best: dict[str, tuple[float | None, dict[str, Any]]] = {}
        for row in raw:
            if not isinstance(row, dict):
                continue
            kind = row.get("data_type")
            if kind is not None and str(kind) != "doc":
                continue
            cid = str(row.get("id") or "").strip()
            if not cid:
                continue
            score = _float(row.get("score"))
            if score is None:
                score = _float(row.get("recall_score"))
            kept = best.get(cid)
            if kept is None or (score is not None and (kept[0] is None or score > kept[0])):
                best[cid] = (score, row)

        hits: list[Hit] = []
        for cid, (score, row) in list(best.items())[:k]:
            text = str(row.get("text") or row.get("data") or "")
            org_id = str(row.get("org_data_id") or "")
            hits.append(Hit(
                chunk_id=cid,
                text=text,
                score=score,
                doc_id=org_id,
                section=_section(text),
                meta={
                    "org_data_id": org_id,
                    "data_type": str(row.get("data_type") or ""),
                    "recall_score": _float(row.get("recall_score")),
                    "text_labels": _labels(row.get("text_labels")),
                },
            ))
        brief = " ".join(query.split())[:40]
        if hits:
            logger.info("zeus search: %d 条 (原始 %d) —— %s", len(hits), len(raw), brief)
        else:
            logger.warning(
                "zeus search: 0 条 —— %s（labels=%s；或命中被 min_score=%s / data_type 过滤）",
                brief, ",".join(labels or []) or "(全部)", self.recall_min_score,
            )
        return hits

    # ── transport ───────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any) -> Any:
        if not self.available:
            raise RagNotConfigured(
                "Zeus 未配置 —— 请设置 RAG_API_BASE 与 ZEUS_APP_ID / "
                "ZEUS_APP_SECRET / ZEUS_INDEX"
            )
        url = self.base_url + path
        request_id = str(uuid.uuid4())
        headers = {
            "x-app-id": self.app_id,
            "x-app-secret": self.app_secret,
            "x-index": self.index,
            "x-request-id": request_id,
        }
        if not self._sem.acquire(timeout=_QUEUE_TIMEOUT_S):
            raise RagUnavailable(f"Zeus 请求排队超时(并发上限 {self.max_concurrency})")
        try:
            resp = self._session.request(
                method, url, headers=headers,
                timeout=timeout or self.timeout_s, **kwargs,
            )
        except requests.RequestException as exc:
            logger.error("zeus %s %s 请求失败 (request-id=%s): %s",
                         method, path, request_id, exc)
            raise RagUnavailable(f"Zeus 不可达({url}): {exc}") from exc
        finally:
            self._sem.release()
        try:
            _raise_status(resp, method, path, request_id=request_id)
            return _unwrap(resp)
        except RagError as exc:
            # Logged here so the failure stays visible when the caller degrades
            # it into "需人工复核".
            logger.error("zeus %s %s 失败 (request-id=%s): %s",
                         method, path, request_id, exc)
            raise


# ── response mapping ────────────────────────────────────────────────────


def _raise_status(
    resp: requests.Response, method: str, path: str, *, request_id: str = ""
) -> None:
    code = resp.status_code
    rid = f" request-id={request_id}" if request_id else ""
    if code in (401, 403):
        raise RagAuthError(
            f"Zeus 鉴权失败({code}){rid}: 检查 ZEUS_APP_ID / ZEUS_APP_SECRET",
            status_code=code,
        )
    if code == 404:
        raise RagNotFound(f"Zeus 未找到({code}){rid}: {method} {path}", status_code=404)
    if code in (408, 409, 429) or code >= 500:
        detail = resp.text[:300].replace("\n", " ").strip()
        raise RagUnavailable(
            f"Zeus 暂不可用({code}){rid}: {method} {path}"
            + (f" — {detail}" if detail else "")
        )
    if code >= 400:
        detail = resp.text[:200].replace("\n", " ")
        raise RagResponseError(
            f"Zeus 返回 {code}: {method} {path} — {detail}", status_code=code
        )


def _unwrap(resp: requests.Response) -> Any:
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        body = resp.json()
    except ValueError as exc:
        raise RagResponseError(f"Zeus 返回非法 JSON: {exc}") from exc
    if not isinstance(body, dict) or "status" not in body:
        detail = resp.text[:200].replace("\n", " ")
        raise RagResponseError(f"Zeus 响应缺少 status: {detail}")
    status = str(body.get("status") or "").strip().lower()
    if status != "success":
        # Zeus answers 200 with a bare {"status":"fail","msg":"error"} for most
        # internal failures, so the whole body is kept.
        detail = resp.text[:500].replace("\n", " ").strip()
        raise RagResponseError(
            f"Zeus 业务错误({resp.status_code}, status={status or 'missing'}): {detail}"
        )
    return body.get("data")


def _labels(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _section(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:80]
    return ""


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
