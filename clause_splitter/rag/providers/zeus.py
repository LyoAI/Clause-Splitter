"""Zeus knowledge-base provider.

All Zeus wire details live here: header auth (``x-app-id`` / ``x-app-secret`` /
``x-index`` / ``x-request-id``), the ``{status,msg,data}`` envelope, the IOBS
upload + document-submit pipeline and ``standard_search``.

Zeus parses, chunks, embeds and indexes a submitted document in the background;
the API exposes no parsed text, chunk listing, document listing or ingest
status. ``parse`` therefore reports unsupported (external regulations fall back
to local extraction), the listing/count methods degrade to empty, and
``ingest`` returns as soon as the document is accepted.

Search hits carry no file name, so ``ingest`` tags each document with a
``doc:<stem>`` label in ``text_labels`` and ``search`` reads it back for
provenance (review page and ESG callback).
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from ..base import RagService
from ..errors import (
    RagAuthError,
    RagNotConfigured,
    RagNotFound,
    RagResponseError,
    RagUnavailable,
)
from ..types import Chunk, CorpusStats, DocInfo, Hit, ParsedDoc

logger = logging.getLogger(__name__)

_UPLOAD = "/api/v1/uns/text_data_process/app/file"
_DOC = "/api/v1/uns/text_data_process/app/doc/{index}"
_DOC_BATCH = "/api/v1/uns/text_data_process/app/doc/batch/{index}"
_SEARCH = "/api/v1/uns/zeus-search/{app_id}/server/standard_search"

# Provenance label written on ingest; Zeus search does not return file names.
_LABEL_PREFIX = "doc:"

# How long a thread may wait for a concurrency slot before giving up.
_QUEUE_TIMEOUT_S = 120.0


class ZeusProvider(RagService):
    name = "zeus"
    supports_parse = False
    supports_document_listing = False
    supports_document_chunks = False

    def __init__(
        self,
        base_url: str = "",
        app_id: str = "",
        app_secret: str = "",
        index: str = "",
        *,
        timeout_s: float = 30.0,
        upload_timeout_s: float = 300.0,
        kb_limit: int = 20,
        max_k: int = 50,
        recall_min_score: float = 0.1,
        rerank_min_score: float = 0.0,
        max_concurrency: int = 2,
        created_by: str = "clause_splitter",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.index = index
        self.timeout_s = timeout_s
        self.upload_timeout_s = upload_timeout_s
        self.kb_limit = max(1, kb_limit)
        self.max_k = max(1, max_k)
        self.recall_min_score = recall_min_score
        self.rerank_min_score = rerank_min_score
        self.max_concurrency = max(1, max_concurrency)
        self.created_by = created_by
        self._session = requests.Session()
        self._sem = threading.BoundedSemaphore(self.max_concurrency)
        # In-process org_data_id -> name fallback (labels are the durable source).
        self._names: dict[str, str] = {}
        self._names_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.app_id and self.app_secret and self.index)

    # ── RagService ──────────────────────────────────────────────────────

    def parse(self, path: Path, doc_id: str) -> ParsedDoc:
        """Zeus parses on ingest but never returns the text — use local extraction."""
        raise RagResponseError("Zeus 不返回解析文本，外规解析走本地抽取")

    def ingest(self, path: Path, name: str, doc_id: str = "") -> DocInfo:
        """Upload the file, submit it for indexing, return without waiting.

        Zeus processes the document asynchronously (parse → chunk → embed →
        index) and has no status endpoint, so ``state="indexed"`` is optimistic
        and ``chunk_count=0`` means unknown.
        """
        path = Path(path)
        url = self._upload(path)
        stem = Path(name).stem or path.stem
        org_id = doc_id or uuid.uuid4().hex
        file_type = (Path(name).suffix or path.suffix).lstrip(".").lower()
        payload = [{
            "org_data_id": org_id,
            "file_name": name,
            "file_type": file_type,
            "file_size": path.stat().st_size,
            "url": url,
            # Search rows carry labels, not file names: this is the only
            # portable way to show the document name on the review page.
            "text_labels": [_LABEL_PREFIX + stem],
            "created_by": self.created_by,
        }]
        data = self._request(
            "POST", _DOC.format(index=quote(self.index, safe="")), json=payload
        )
        remote_id = str(data or org_id)
        with self._names_lock:
            self._names[remote_id] = stem
            self._names.setdefault(org_id, stem)
        return DocInfo(doc_id=remote_id, name=stem, chunk_count=0, state="indexed")

    def search(self, query: str, k: int) -> list[Hit]:
        """One standard search; rows are deduped by chunk id, best score first."""
        k = max(1, min(int(k or 0), self.max_k))
        body: dict[str, Any] = {
            "session_id": str(uuid.uuid4()),
            "query": query,
            "kb": [{
                "alias": self.index,
                "filter": {
                    "data_type": ["doc"],
                    "text_labels": [],
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
            # A 404 here means the search capability was never enabled for this
            # app (per the Zeus doc), i.e. a deployment problem, not "no hits".
            raise RagNotConfigured(
                "Zeus 检索未配置(404) —— 请联系 RAG 团队为该应用开通检索"
            ) from exc

        best: dict[str, tuple[float | None, dict[str, Any]]] = {}
        for row in rows if isinstance(rows, list) else []:
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
            doc = self._doc_name(row)
            hits.append(Hit(
                chunk_id=cid,
                text=text,
                score=score,
                doc_id=doc,
                section=_section(text),
                meta={
                    "doc": doc,
                    "org_data_id": str(row.get("org_data_id") or ""),
                    "data_type": str(row.get("data_type") or ""),
                    "recall_score": _float(row.get("recall_score")),
                    "text_labels": _labels(row.get("text_labels")),
                },
            ))
        return hits

    def fetch_chunks(self, doc_id: str) -> list[Chunk]:
        """No per-document chunk endpoint — the tool is hidden by capability."""
        raise RagResponseError("Zeus 不提供按文档列切片的接口")

    def documents(self) -> list[DocInfo]:
        """No document-listing endpoint; corpus inventory is unknown."""
        return []

    def stats(self) -> CorpusStats:
        """No corpus counters or version endpoint; report unknown (empty)."""
        return CorpusStats()

    def delete(self, doc_id: str) -> bool:
        try:
            self._request(
                "DELETE", _DOC_BATCH.format(index=quote(self.index, safe="")),
                json=[doc_id],
            )
        except RagNotFound:
            return True  # already gone
        return True

    # ── transport ───────────────────────────────────────────────────────

    def _upload(self, path: Path) -> str:
        with path.open("rb") as fh:
            data = self._request(
                "POST",
                _UPLOAD,
                files={"file": (path.name, fh, "application/octet-stream")},
                timeout=self.upload_timeout_s,
            )
        if not isinstance(data, str) or not data.strip():
            raise RagResponseError(f"Zeus 上传未返回文件 URL: {path.name}")
        return data.strip()

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        if not self.available:
            raise RagNotConfigured(
                "Zeus 未配置 —— 请设置 RAG_API_BASE 与 ZEUS_APP_ID / "
                "ZEUS_APP_SECRET / ZEUS_INDEX"
            )
        url = self.base_url + path
        headers = {
            "x-app-id": self.app_id,
            "x-app-secret": self.app_secret,
            "x-index": self.index,
            "x-request-id": str(uuid.uuid4()),
        }
        if not self._sem.acquire(timeout=_QUEUE_TIMEOUT_S):
            raise RagUnavailable(
                f"Zeus 请求排队超时(并发上限 {self.max_concurrency})"
            )
        try:
            resp = self._session.request(
                method, url, headers=headers,
                timeout=timeout or self.timeout_s, **kwargs,
            )
        except requests.RequestException as exc:
            raise RagUnavailable(f"Zeus 不可达({url}): {exc}") from exc
        finally:
            self._sem.release()
        _raise_status(resp, method, path)
        return _unwrap(resp)

    def _doc_name(self, row: dict[str, Any]) -> str:
        for label in _labels(row.get("text_labels")):
            if label.startswith(_LABEL_PREFIX) and len(label) > len(_LABEL_PREFIX):
                return label[len(_LABEL_PREFIX):]
        org = str(row.get("org_data_id") or "")
        if org:
            with self._names_lock:
                name = self._names.get(org)
            if name:
                return name
        return org or str(row.get("id") or "")


# ── response mapping ────────────────────────────────────────────────────


def _raise_status(resp: requests.Response, method: str, path: str) -> None:
    code = resp.status_code
    if code in (401, 403):
        raise RagAuthError(
            f"Zeus 鉴权失败({code}): 检查 ZEUS_APP_ID / ZEUS_APP_SECRET",
            status_code=code,
        )
    if code == 404:
        raise RagNotFound(f"Zeus 未找到: {method} {path}", status_code=404)
    if code in (408, 409, 429) or code >= 500:
        raise RagUnavailable(f"Zeus 暂不可用({code}): {method} {path}")
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
        raise RagResponseError(
            f"Zeus 业务错误: {body.get('msg') or status or 'unknown'}"
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
