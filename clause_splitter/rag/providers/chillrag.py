"""ChillRAG provider.

All ChillRAG wire details live here: cookie login, the ``{code,msg,data}``
envelope, folder/file ids, upload polling and the vendor recall query string.
Business code never sees any of it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

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

_LOGIN = "/space/api/passport/login"
_RESOURCES = "/space/api/service/asset-file/resource"
_WEB_UPLOAD = "/space/api/service/asset-file/web-upload"
_SEGMENTS = "/space/api/service/asset-file/segments/{file_id}"
_RECALL = "/space/api/service/llmapi/ht/normal_rag_recall_without_doc_group"
_UI_UPLOAD = "/api/upload"
_UI_STATUS = "/api/upload/status"
_UI_ENSURE = "/api/upload/ensure"

# What the remote parser accepts; anything else is parsed locally instead.
SUPPORTED_SUFFIXES = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg", ".md", ".markdown"}
)

_POLL_S = 2.0


class ChillRagProvider(RagService):
    name = "chillrag"

    def __init__(
        self,
        base_url: str = "",
        username: str = "",
        password: str = "",
        *,
        timeout_s: float = 30.0,
        ingest_timeout_s: float = 1800.0,
        corpus: str = "clause_splitter_internal",
        parse_corpus: str = "clause_splitter_parse",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.timeout_s = timeout_s
        self.ingest_timeout_s = ingest_timeout_s
        self.corpus = corpus
        self.parse_corpus = parse_corpus
        self._session = requests.Session()
        self._login_lock = threading.Lock()
        self._logged_in = False

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    # ── RagService ──────────────────────────────────────────────────────

    def parse(self, path: Path, doc_id: str) -> ParsedDoc:
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise RagResponseError(f"ChillRAG 不支持解析 {suffix or '(无后缀)'} 文件，回退本地抽取")
        # Content-addressed name: the parse corpus accumulates uploads and has
        # no delete endpoint, so repeated parses of one file overwrite one entry.
        wire = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:16] + suffix
        source_id, file_id = self._upload(self.parse_corpus, path, wire)
        self._wait_ingest(source_id, wire, doc_id=doc_id)
        rows = self._request("GET", _SEGMENTS.format(file_id=file_id)) or []
        text = "\n\n".join(
            part for _, part in sorted(
                ((_segment_index(row), _segment_text(row)) for row in rows),
                key=lambda item: item[0],
            ) if part
        )
        if not text.strip():
            raise RagResponseError(f"ChillRAG 解析结果为空: {path.name}")
        return ParsedDoc(doc_id=doc_id, text=text, meta={"provider": self.name, "file_id": file_id})

    def ingest(self, path: Path, name: str, doc_id: str = "") -> DocInfo:
        path = Path(path)
        source_id, _ = self._upload(self.corpus, path, path.name)
        chunks = self._wait_ingest(source_id, path.name, doc_id=doc_id)
        stem = Path(path.name).stem
        return DocInfo(doc_id=stem, name=stem, chunk_count=chunks, state="indexed")

    def search(self, query: str, k: int) -> list[Hit]:
        source_id = self._source_id(self.corpus)
        if source_id is None:
            return []
        rows = self._request(
            "POST",
            _RECALL,
            params={
                "question": query,
                "source_ids": str(source_id),
                "origin_ids": "[]",
                "context_trace_ids": "",
            },
        ) or []
        best: dict[str, tuple[float | None, dict[str, Any]]] = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("source"):
                continue
            chunk_id = str(row["source"])
            score = _float(row.get("score"))
            kept = best.get(chunk_id)
            if kept is None or (score is not None and score > (kept[0] or 0.0)):
                best[chunk_id] = (score, row)
        hits = []
        for chunk_id, (score, row) in list(best.items())[:k]:
            text = str(row.get("segment") or "")
            doc = _doc_name(row)
            hits.append(Hit(
                chunk_id=chunk_id, text=text, score=score, doc_id=doc,
                section=_section(text), meta={"doc": doc},
            ))
        return hits

    def fetch_chunks(self, doc_id: str) -> list[Chunk]:
        row = self._file_row(doc_id)
        file_id = _int(row.get("fileId"))
        if not file_id:
            raise RagNotFound(f"ChillRAG 未找到文档: {doc_id}")
        rows = self._request("GET", _SEGMENTS.format(file_id=file_id)) or []
        chunks = []
        for segment in rows:
            index = _segment_index(segment)
            text = _segment_text(segment)
            chunks.append(Chunk(
                chunk_id=f"{file_id}#{index}", doc_id=doc_id, order=index, text=text,
                meta={"doc": doc_id, "section": _section(text)},
            ))
        return sorted(chunks, key=lambda chunk: chunk.order)

    def documents(self) -> list[DocInfo]:
        status = self._status(self.corpus)
        job = str(status.get("state") or "")
        docs = []
        for row in status.get("files") or []:
            name = str(row.get("name") or "")
            chunks = _int(row.get("chunks"))
            state = "indexed" if chunks else ("indexing" if job == "processing" else "failed")
            docs.append(DocInfo(
                doc_id=Path(name).stem, name=Path(name).stem,
                chunk_count=chunks, state=state,
            ))
        return docs

    def stats(self) -> CorpusStats:
        status = self._status(self.corpus)
        files = status.get("files") or []
        version = ""
        if files:
            inventory = ";".join(sorted(
                f"{row.get('name')}:{row.get('fileId')}:{row.get('chunks')}" for row in files
            ))
            version = "chillrag-v1-" + hashlib.sha1(inventory.encode()).hexdigest()[:16]
        return CorpusStats(
            doc_count=_int(status.get("documents")),
            chunk_count=_int(status.get("chunks")),
            version=version,
        )

    # ── transport ───────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        retry_auth: bool = True,
        **kwargs: Any,
    ) -> Any:
        if not self.available:
            raise RagNotConfigured("RAG_API_BASE 未配置 —— 请在 .env 设置远端 RAG 服务地址")
        self._login()
        url = self.base_url + path
        try:
            resp = self._session.request(
                method, url, timeout=timeout or self.timeout_s, **kwargs
            )
        except requests.RequestException as exc:
            raise RagUnavailable(f"RAG 服务不可达({url}): {exc}") from exc
        # Uploads are not replayed (the file handle is spent); a fresh login
        # before the request makes an expired cookie there unlikely.
        if resp.status_code == 401 and retry_auth and self.username:
            self._login(force=True)
            return self._request(
                method, path, timeout=timeout, retry_auth=False, **kwargs
            )
        _raise_status(resp, method, path)
        return _unwrap(resp)

    def _login(self, force: bool = False) -> None:
        if self._logged_in and not force:
            return
        with self._login_lock:
            if self._logged_in and not force:
                return
            if not self.username:
                self._logged_in = True
                return
            try:
                resp = self._session.post(
                    self.base_url + _LOGIN,
                    json={"username": self.username, "passwd": self.password},
                    timeout=self.timeout_s,
                )
            except requests.RequestException as exc:
                raise RagUnavailable(f"RAG 登录失败: {exc}") from exc
            _raise_status(resp, "POST", _LOGIN)
            _unwrap(resp)
            self._logged_in = True

    # ── corpus helpers ──────────────────────────────────────────────────

    def _source_id(self, folder: str) -> int | None:
        rows = self._request("GET", _RESOURCES) or []
        for row in rows:
            if isinstance(row, dict) and row.get("resourceName") == folder:
                return _int(row.get("sourceId")) or None
        return None

    def _ensure_source(self, folder: str) -> int | None:
        source_id = self._source_id(folder)
        if source_id is not None:
            return source_id
        try:
            return _int(self._request("PUT", _RESOURCES, json={"resourceName": folder})) or None
        except RagAuthError:
            # Creating a source is admin-only; the UI upload path creates it.
            return None

    def _upload(self, folder: str, path: Path, wire_name: str) -> tuple[int, int]:
        """Upload one file and return ``(source_id, file_id)``."""
        source_id = self._ensure_source(folder)
        with path.open("rb") as fh:
            files = [("files", (wire_name, fh, "application/octet-stream"))]
            if source_id is not None:
                data = self._request(
                    "POST", _WEB_UPLOAD, params={"sourceId": source_id},
                    files=files, retry_auth=False,
                )
            else:
                data = self._request(
                    "POST", _UI_UPLOAD, params={"folder": folder},
                    files=files, retry_auth=False,
                )
        if isinstance(data, dict):
            source_id = _int(data.get("sourceId")) or source_id or 0
            rows = data.get("files") or []
            file_id = _int(rows[0].get("fileId")) if rows else 0
        else:
            file_id = _int(data[0]) if isinstance(data, list) and data else 0
        if not source_id or not file_id:
            raise RagResponseError(f"ChillRAG 上传未返回 source/file id: {wire_name}")
        return source_id, file_id

    def _wait_ingest(self, source_id: int, wire_name: str, *, doc_id: str = "") -> int:
        """Poll until the folder's ingest finishes; return the file's chunk count."""
        deadline = time.monotonic() + self.ingest_timeout_s
        ensured = False
        while True:
            status = self._request("GET", _UI_STATUS, params={"sourceId": source_id}) or {}
            state = str(status.get("state") or "")
            row = next(
                (r for r in status.get("files") or [] if r.get("name") == wire_name),
                None,
            )
            chunks = _int(row.get("chunks")) if row else 0
            if state == "failed":
                raise RagUnavailable(f"ChillRAG 入库失败: {doc_id or wire_name}")
            if chunks:
                return chunks
            if state == "processing":
                pass
            elif not ensured:
                # The folder can hold files without a prepared corpus.
                self._request("POST", _UI_ENSURE, params={"sourceId": source_id})
                ensured = True
            else:
                raise RagResponseError(f"ChillRAG 入库后没有切片: {doc_id or wire_name}")
            if time.monotonic() >= deadline:
                raise RagUnavailable(
                    f"ChillRAG 入库超时({self.ingest_timeout_s:.0f}s): {doc_id or wire_name}"
                )
            time.sleep(_POLL_S)

    def _status(self, folder: str) -> dict[str, Any]:
        source_id = self._source_id(folder)
        if source_id is None:
            return {}
        return self._request("GET", _UI_STATUS, params={"sourceId": source_id}) or {}

    def _file_row(self, doc_id: str) -> dict[str, Any]:
        for row in self._status(self.corpus).get("files") or []:
            name = str(row.get("name") or "")
            if Path(name).stem == doc_id or str(row.get("fileId")) == doc_id:
                return row
        raise RagNotFound(f"ChillRAG 未找到文档: {doc_id}")


# ── response mapping ────────────────────────────────────────────────────


def _raise_status(resp: requests.Response, method: str, path: str) -> None:
    code = resp.status_code
    if code in (401, 403):
        raise RagAuthError(
            f"RAG 鉴权失败({code}): 检查 RAG_API_USER / RAG_API_PASSWORD",
            status_code=code,
        )
    if code == 404:
        raise RagNotFound(f"RAG 未找到: {method} {path}", status_code=404)
    if code == 409 or code >= 500:
        raise RagUnavailable(f"RAG 暂不可用({code}): {method} {path}")
    if code >= 400:
        detail = resp.text[:200].replace("\n", " ")
        raise RagResponseError(
            f"RAG 返回 {code}: {method} {path} — {detail}", status_code=code
        )


def _unwrap(resp: requests.Response) -> Any:
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        body = resp.json()
    except ValueError as exc:
        raise RagResponseError(f"RAG 返回非法 JSON: {exc}") from exc
    if isinstance(body, dict) and "code" in body:
        if body.get("code") != 0:
            raise RagResponseError(f"RAG 业务错误({body.get('code')}): {body.get('msg')}")
        return body.get("data")
    return body


def _segment_text(segment: Any) -> str:
    content = (segment or {}).get("segment", {}).get("content", "")
    try:
        return str(json.loads(content).get("content") or "")
    except (TypeError, ValueError, AttributeError):
        return str(content or "")


def _segment_index(segment: Any) -> int:
    return _int(((segment or {}).get("segment") or {}).get("index"))


def _doc_name(row: dict[str, Any]) -> str:
    meta = str(row.get("metadata") or "")
    if meta.startswith("文件名称:"):
        return meta[len("文件名称:"):]
    return meta or str(row.get("source") or "").split("#", 1)[0]


def _section(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:80]
    return ""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
