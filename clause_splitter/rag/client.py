"""HTTP client for the external RAG service.

Wire contract: ``assets/rag-service-contract.md``. This module is the *only*
place that knows request/response field names — when the internal team's API
differs, adapt the mapping here and leave business code untouched.

The client is synchronous (``requests``): the ReAct agent already executes tool
calls in worker threads and async callers wrap RAG calls in
``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .errors import (
    RagAuthError,
    RagNotFound,
    RagNotConfigured,
    RagResponseError,
    RagUnavailable,
)
from .types import Chunk, CorpusStats, DocInfo, Hit, ParsedDoc, RagCapabilities

logger = logging.getLogger(__name__)

_JSON_HEADERS = {"Accept": "application/json"}


class HttpRagClient:
    """REST implementation of :class:`RagService`."""

    name = "remote"

    def __init__(
        self,
        base_url: str = "",
        *,
        api_key: str = "",
        timeout_s: float = 30.0,
        parse_timeout_s: float = 300.0,
        capabilities: RagCapabilities | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.parse_timeout_s = parse_timeout_s
        self._capabilities = capabilities or RagCapabilities()
        self._session = requests.Session()
        # Retry idempotent reads only; uploads must not be replayed blindly.
        self._session.mount(
            "http://",
            HTTPAdapter(max_retries=Retry(
                total=2, backoff_factor=0.5, status_forcelist=(502, 503, 504),
                allowed_methods=frozenset({"GET"}),
            )),
        )
        self._session.mount(
            "https://",
            HTTPAdapter(max_retries=Retry(
                total=2, backoff_factor=0.5, status_forcelist=(502, 503, 504),
                allowed_methods=frozenset({"GET"}),
            )),
        )

    # ── availability / capabilities ─────────────────────────────────────

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    def capabilities(self) -> RagCapabilities:
        return self._capabilities

    # ── parse ───────────────────────────────────────────────────────────

    def parse(self, path: Path, doc_id: str) -> ParsedDoc:
        """Upload one external document and return its parsed full text."""
        path = Path(path)
        with path.open("rb") as fh:
            resp = self._request(
                "POST",
                "/parse",
                files={"file": (path.name, fh, "application/octet-stream")},
                data={"doc_id": doc_id},
                timeout=self.parse_timeout_s,
            )
        body = _decode(resp)
        if isinstance(body, str):  # plain-text parse response
            return ParsedDoc(doc_id=doc_id, text=body)
        if not isinstance(body, dict):
            raise RagResponseError("parse: expected a JSON object or plain text")
        text = _first_str(body, "text", "content", "full_text")
        markdown = _first_str(body, "markdown", "md")
        blocks = body.get("blocks")
        has_blocks = isinstance(blocks, list) and bool(blocks)
        if not text.strip() and not markdown.strip() and not has_blocks:
            raise RagResponseError("parse: response contains no text, markdown or blocks")
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        return ParsedDoc(
            doc_id=str(body.get("doc_id") or doc_id),
            text=text,
            markdown=markdown,
            blocks=blocks if has_blocks else None,
            meta=meta,
        )

    # ── ingest / delete ─────────────────────────────────────────────────

    def ingest(self, path: Path, name: str, doc_id: str = "") -> DocInfo:
        """Upsert one internal document; the service owns parsing and chunking."""
        path = Path(path)
        with path.open("rb") as fh:
            resp = self._request(
                "POST",
                "/documents",
                files={"file": (path.name, fh, "application/octet-stream")},
                data={"name": name, "doc_id": doc_id},
                timeout=self.parse_timeout_s,
            )
        body = _decode(resp)
        if not isinstance(body, dict):
            raise RagResponseError("ingest: expected a JSON object")
        return _doc_info(body, fallback_id=doc_id or name)

    def delete(self, doc_id: str) -> None:
        try:
            self._request("DELETE", f"/documents/{quote(doc_id, safe='')}")
        except RagNotFound:
            logger.warning("RAG delete: document %s not found (already gone)", doc_id)

    # ── search / inspect ────────────────────────────────────────────────

    def search(self, query: str, k: int) -> list[Hit]:
        resp = self._request("POST", "/search", json={"query": query, "top_k": k})
        rows = _rows(_decode(resp), "hits")
        return [_hit(row) for row in rows]

    def fetch_chunks(self, doc_id: str) -> list[Chunk]:
        resp = self._request("GET", f"/documents/{quote(doc_id, safe='')}/chunks")
        rows = _rows(_decode(resp), "chunks")
        return [_chunk(row, fallback_doc=doc_id) for row in rows]

    def documents(self) -> list[DocInfo]:
        resp = self._request("GET", "/documents")
        rows = _rows(_decode(resp), "documents")
        return [_doc_info(row) for row in rows]

    def stats(self) -> CorpusStats:
        body = _decode(self._request("GET", "/stats"))
        if not isinstance(body, dict):
            raise RagResponseError("stats: expected a JSON object")
        return CorpusStats(
            doc_count=_int(body.get("doc_count")),
            chunk_count=_int(body.get("chunk_count")),
            version=str(body.get("version") or body.get("index_version") or ""),
        )

    # ── transport ───────────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        if not self.available:
            raise RagNotConfigured(
                "RAG_API_BASE 未配置 —— 请在 .env 设置远端 RAG 服务地址"
            )
        url = self.base_url + path
        headers = dict(_JSON_HEADERS)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        kwargs.setdefault("timeout", self.timeout_s)
        try:
            resp = self._session.request(method, url, headers=headers, **kwargs)
        except requests.Timeout as exc:
            raise RagUnavailable(f"RAG 服务超时({kwargs['timeout']}s): {method} {path}") from exc
        except requests.RequestException as exc:
            raise RagUnavailable(f"RAG 服务不可达({url}): {exc}") from exc

        if resp.status_code in (401, 403):
            raise RagAuthError(
                f"RAG 鉴权失败({resp.status_code}): 检查 RAG_API_KEY",
                status_code=resp.status_code,
            )
        if resp.status_code == 404:
            raise RagNotFound(f"RAG 未找到: {method} {path}", status_code=404)
        if resp.status_code >= 400:
            detail = resp.text[:300].replace("\n", " ")
            raise RagResponseError(
                f"RAG 返回 {resp.status_code}: {method} {path} — {detail}",
                status_code=resp.status_code,
            )
        return resp


# ── response mapping helpers ────────────────────────────────────────────


def _decode(resp: requests.Response) -> Any:
    """JSON body when possible, else the raw text (empty → None)."""
    if resp.status_code == 204 or not resp.content:
        return None
    ctype = resp.headers.get("Content-Type", "")
    if "json" in ctype.lower():
        try:
            return resp.json()
        except ValueError as exc:
            raise RagResponseError(f"RAG 返回非法 JSON: {exc}") from exc
    body = resp.text
    try:
        return resp.json()
    except ValueError:
        return body


def _rows(body: Any, key: str) -> list[dict[str, Any]]:
    """Accept a bare list or the documented wrapper key."""
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        rows = next(
            (body[k] for k in (key, "data", "items", "results") if isinstance(body.get(k), list)),
            None,
        )
        if rows is None:
            raise RagResponseError(f"RAG 响应缺少列表字段 {key!r}")
    else:
        raise RagResponseError(f"RAG 响应应为列表/对象，实际为 {type(body).__name__}")
    return [row for row in rows if isinstance(row, dict)]


def _first_str(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _hit(row: dict[str, Any]) -> Hit:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    chunk_id = str(row.get("chunk_id") or row.get("id") or "")
    doc_id = str(row.get("doc_id") or row.get("doc") or row.get("document_name") or "")
    section = str(row.get("section") or meta.get("section") or "")
    if not doc_id and "#" in chunk_id:
        doc_id = chunk_id.split("#", 1)[0]
    score = row.get("score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return Hit(
        chunk_id=chunk_id,
        text=_first_str(row, "text", "content", "snippet"),
        score=score,
        doc_id=doc_id,
        section=section,
        meta=meta,
    )


def _chunk(row: dict[str, Any], fallback_doc: str = "") -> Chunk:
    hit = _hit(row)
    doc_id = hit.doc_id or str(row.get("doc_id") or fallback_doc)
    order = row.get("order", row.get("chunk_index", row.get("index", 0)))
    try:
        order = int(order)
    except (TypeError, ValueError):
        order = 0
    return Chunk(
        chunk_id=hit.chunk_id,
        doc_id=doc_id,
        order=order,
        text=hit.text,
        meta=hit.meta,
    )


def _doc_info(row: dict[str, Any], fallback_id: str = "") -> DocInfo:
    doc_id = str(row.get("doc_id") or row.get("id") or fallback_id)
    return DocInfo(
        doc_id=doc_id,
        name=str(row.get("name") or row.get("file_name") or row.get("filename") or doc_id),
        chunk_count=_int(row.get("chunk_count")),
        state=str(row.get("state") or row.get("status") or ""),
    )
