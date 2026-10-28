"""Zeus knowledge-base provider.

All Zeus wire details live here: header auth (``x-app-id`` / ``x-app-secret`` /
``x-index`` / ``x-request-id``), the ``{status,msg,data}`` envelope, the IOBS
upload + document-submit pipeline and ``standard_search``.

Zeus parses, chunks, embeds and indexes a submitted document in the background;
the API exposes no parsed text, chunk listing, document listing or ingest
status. ``parse`` therefore reports unsupported (external regulations fall back
to local extraction), the listing/count methods degrade to empty, and
``ingest`` returns as soon as the document is accepted.

Labels are what separate corpora in one index: Zeus indexes only documents that
carry ``text_labels``, and ``x-app-id`` / ``x-index`` scope the whole knowledge
base. ``ingest`` therefore writes the caller's corpus label(s) plus a
``doc:<stem>`` label, which ``search`` reads back as the document name (hits
carry no file name). ``search`` filters on ``search_labels``; an empty list is
sent as ``text_labels=[]``, which searches the whole knowledge base.
"""

from __future__ import annotations

import mimetypes
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from clause_splitter.utils import get_logger

from ..base import RagService
from ..errors import (
    RagAuthError,
    RagError,
    RagNotConfigured,
    RagNotFound,
    RagResponseError,
    RagUnavailable,
)
from ..types import Chunk, CorpusStats, DocInfo, Hit, ParsedDoc

logger = get_logger()

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
        search_labels: list[str] | None = None,
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
        # Empty = text_labels=[], i.e. no label filter (whole knowledge base).
        self.search_labels = list(search_labels or [])
        self._session = requests.Session()
        self._sem = threading.BoundedSemaphore(self.max_concurrency)
        # In-process org_data_id -> name fallback (labels are the durable source).
        self._names: dict[str, str] = {}
        self._names_lock = threading.Lock()
        logger.info(
            "zeus provider ready: base=%s index=%s concurrency=%d search_labels=%s available=%s",
            self.base_url or "-", self.index or "-", self.max_concurrency,
            ",".join(self.search_labels) or "(none)", self.available,
        )

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.app_id and self.app_secret and self.index)

    # ── RagService ──────────────────────────────────────────────────────

    def parse(self, path: Path, doc_id: str) -> ParsedDoc:
        """Zeus parses on ingest but never returns the text — use local extraction."""
        raise RagResponseError("Zeus 不返回解析文本，外规解析走本地抽取")

    def ingest(
        self, path: Path, name: str, doc_id: str = "", *, labels: list[str] | None = None
    ) -> DocInfo:
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
        labels = [*(labels or []), _LABEL_PREFIX + stem]
        payload = {
            "org_data_id": org_id,
            "file_name": name,
            "file_type": file_type,
            "file_size": path.stat().st_size,
            "url": url,
            # Corpus label(s) first, then the name label: search rows carry
            # labels rather than file names, so "doc:<stem>" is the only
            # portable way to show the document name on the review page.
            "text_labels": labels,
            "created_by": self.created_by,
        }
        logger.info(
            "zeus ingest: 提交入库 %s (org_data_id=%s, %d bytes, labels=%s)",
            name, org_id, payload["file_size"], ",".join(labels),
        )
        # A single object, not a one-element array: Zeus answers 500
        # `readObjectStart: expect { or n, but found [` otherwise.
        data = self._request(
            "POST", _DOC.format(index=quote(self.index, safe="")), json=payload
        )
        remote_id = str(data or org_id)
        logger.success(
            "zeus ingest: %s 入库已受理 (org_data_id=%s, labels=%s)，"
            "Zeus 后台解析/分块/embedding 中，完成后才可检索",
            name, remote_id, ",".join(labels),
        )
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
                    "text_labels": list(self.search_labels),
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
        brief = " ".join(query.split())[:40]
        if hits:
            logger.info("zeus search: %d 条 (原始 %d) —— %s", len(hits), len(raw), brief)
        else:
            logger.warning(
                "zeus search: 0 条 —— %s（labels=%s；文档可能仍在后台解析/索引，"
                "或命中被 min_score=%s / data_type 过滤）",
                brief, ",".join(self.search_labels) or "(全部)", self.recall_min_score,
            )
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
        logger.info("zeus delete: %s 已删除", doc_id)
        return True

    # ── transport ───────────────────────────────────────────────────────

    def _upload(self, path: Path) -> str:
        logger.info("zeus upload: 上传文件中 %s (%.1f KB)", path.name, path.stat().st_size / 1024)
        # The multipart part carries the real MIME type, like a browser/Postman
        # POST. Sending application/octet-stream for every file makes Zeus reject
        # the upload with a bare {"status":"fail","msg":"error"}.
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        logger.info("zeus upload: %s part Content-Type=%s", path.name, content_type)
        try:
            with path.open("rb") as fh:
                data = self._request(
                    "POST",
                    _UPLOAD,
                    files={"file": (path.name, fh, content_type)},
                    timeout=self.upload_timeout_s,
                )
        except RagError as exc:
            logger.error("zeus upload: 上传失败 %s —— %s", path.name, exc)
            raise
        if not isinstance(data, str) or not data.strip():
            logger.error("zeus upload: %s 上传成功但未返回文件 URL", path.name)
            raise RagResponseError(f"Zeus 上传未返回文件 URL: {path.name}")
        logger.info("zeus upload: %s 上传成功 → %s", path.name, data.strip())
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
        request_id = str(uuid.uuid4())
        headers = {
            "x-app-id": self.app_id,
            "x-app-secret": self.app_secret,
            "x-index": self.index,
            "x-request-id": request_id,
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
            logger.error("zeus %s %s 请求失败 (request-id=%s): %s",
                         method, path, request_id, exc)
            raise RagUnavailable(f"Zeus 不可达({url}): {exc}") from exc
        finally:
            self._sem.release()
        logger.debug("zeus %s %s → %s", method, path, resp.status_code)
        try:
            _raise_status(resp, method, path, request_id=request_id)
            return _unwrap(resp)
        except RagError as exc:
            # Logged here so the failure is visible even when a per-clause
            # caller degrades it into "需人工复核". Covers the envelope case
            # too: a 200 with status=fail only surfaces here.
            logger.error("zeus %s %s 失败 (request-id=%s): %s",
                         method, path, request_id, exc)
            raise

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
        # internal failures. msg alone is useless, so the whole body is kept.
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
