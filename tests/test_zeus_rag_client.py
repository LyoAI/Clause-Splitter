"""Zeus provider mapping — wire behaviour with a stub HTTP session."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import pytest

from clause_splitter.rag.errors import (
    RagAuthError,
    RagNotConfigured,
    RagNotFound,
    RagResponseError,
    RagUnavailable,
)
from clause_splitter.rag.providers.zeus import ZeusProvider
from clause_splitter.rag.types import CorpusStats


class _Resp:
    """Minimal requests.Response stand-in."""

    def __init__(self, status: int = 200, body=None, *, raw: str | None = None) -> None:
        self.status_code = status
        self._body = body
        if raw is not None:
            self.text = raw
            self.content = b"x"
        else:
            self.content = b"x" if body is not None else b""
            self.text = json.dumps(body, ensure_ascii=False) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("response is not JSON")
        return self._body


class _Session:
    """Stub requests.Session that routes by call."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, timeout=None, **kwargs):
        call = {"method": method, "url": url, "headers": headers,
                "timeout": timeout, **kwargs}
        self.calls.append(call)
        return self.handler(method, url, headers, kwargs)


def _ok(data):
    return _Resp(200, {"status": "success", "msg": "", "data": data})


def _provider(session: _Session, **kwargs) -> ZeusProvider:
    provider = ZeusProvider(
        "http://zeus.local", "jt_law", "secret", "idx_1",
        timeout_s=1, upload_timeout_s=5, kb_limit=20, max_k=50, **kwargs,
    )
    provider._session = session
    return provider


def _upload_ok(url: str = "http://iobs/file.docx"):
    return _ok(url)


# ── configuration & capabilities ────────────────────────────────────────


def test_available_requires_every_credential():
    assert ZeusProvider("", "a", "b", "c").available is False
    assert ZeusProvider("http://x", "", "b", "c").available is False
    assert ZeusProvider("http://x", "a", "", "c").available is False
    assert ZeusProvider("http://x", "a", "b", "").available is False
    assert ZeusProvider("http://x", "a", "b", "c").available is True


def test_unsupported_endpoints_degrade():
    session = _Session(lambda *a: pytest.fail("no HTTP expected"))
    provider = _provider(session)

    assert provider.supports_parse is False
    assert provider.supports_document_listing is False
    assert provider.supports_document_chunks is False
    with pytest.raises(RagResponseError):
        provider.parse(Path("外规.docx"), "外规")
    with pytest.raises(RagResponseError):
        provider.fetch_chunks("doc-1")
    assert provider.documents() == []
    assert provider.stats() == CorpusStats()
    assert session.calls == []


# ── search ──────────────────────────────────────────────────────────────


def test_search_sends_headers_and_documented_body():
    session = _Session(lambda *a: _ok([]))
    provider = _provider(session)

    provider.search("数据安全", 3)
    provider.search("数据安全", 3)
    first, second = session.calls

    assert first["method"] == "POST"
    assert first["url"] == (
        "http://zeus.local/api/v1/uns/zeus-search/jt_law/server/standard_search"
    )
    assert first["headers"]["x-app-id"] == "jt_law"
    assert first["headers"]["x-app-secret"] == "secret"
    assert first["headers"]["x-index"] == "idx_1"
    uuid.UUID(first["headers"]["x-request-id"])
    assert first["headers"]["x-request-id"] != second["headers"]["x-request-id"]

    body = first["json"]
    uuid.UUID(body["session_id"])
    assert body["query"] == "数据安全"
    assert body["kb"] == [{
        "alias": "idx_1",
        "filter": {"data_type": ["doc"], "text_labels": [], "min_score": 0.1},
        "limit": 20,
    }]
    assert body["limit"] == 3
    assert "min_score" not in body  # 0 = let the service apply its default


def test_search_includes_rerank_floor_when_configured():
    session = _Session(lambda *a: _ok([]))
    _provider(session, rerank_min_score=0.8).search("q", 2)
    assert session.calls[0]["json"]["min_score"] == 0.8


def test_search_maps_rows_and_provenance():
    rows = [
        {"id": "c1", "org_data_id": "o1", "text": "", "data": "第一条 甲",
         "score": 0.0, "recall_score": 0.9, "text_labels": ["doc:办法A"]},
        {"id": "faq-1", "data_type": "doc-faq", "text": "问", "data": "答"},
        {"id": "c2", "org_data_id": "o2", "data_type": "doc", "text": "第二条 乙",
         "score": None, "recall_score": 0.7, "text_labels": []},
    ]
    hits = _provider(_Session(lambda *a: _ok(rows))).search("q", 5)

    assert [(h.chunk_id, h.score, h.doc_id) for h in hits] == [
        ("c1", 0.0, "办法A"),
        ("c2", 0.7, "o2"),
    ]
    assert hits[0].text == "第一条 甲"  # text empty -> data fallback
    assert hits[0].section == "第一条 甲"
    assert hits[0].meta["org_data_id"] == "o1"
    assert hits[0].meta["text_labels"] == ["doc:办法A"]


def test_search_dedupes_keeps_best_score_and_truncates():
    rows = [
        {"id": "c1", "text": "甲", "score": 0.4},
        {"id": "c1", "text": "甲", "score": 0.9},
        {"id": "c2", "text": "乙", "score": 0.7},
        {"id": "c3", "text": "丙", "score": 0.6},
    ]
    hits = _provider(_Session(lambda *a: _ok(rows))).search("q", 2)
    assert [(h.chunk_id, h.score) for h in hits] == [("c1", 0.9), ("c2", 0.7)]


def test_search_404_means_search_not_configured():
    session = _Session(lambda *a: _Resp(404, {"status": "fail", "msg": "no route"}))
    with pytest.raises(RagNotConfigured):
        _provider(session).search("q", 1)


# ── ingest ──────────────────────────────────────────────────────────────


def test_ingest_uploads_then_submits(tmp_path):
    src = tmp_path / "数据安全管理办法.docx"
    src.write_bytes(b"docx-bytes")

    def handler(method, url, headers, kwargs):
        if url.endswith("/text_data_process/app/file"):
            return _upload_ok()
        if url.endswith("/text_data_process/app/doc/idx_1"):
            return _ok("remote-9")
        raise AssertionError(url)

    session = _Session(handler)
    info = _provider(session).ingest(src, "数据安全管理办法.docx", "doc-7")

    assert (info.doc_id, info.name, info.chunk_count, info.state) == (
        "remote-9", "数据安全管理办法", 0, "indexed"
    )
    upload, submit = session.calls
    assert upload["method"] == "POST"
    assert upload["files"]["file"][0] == "数据安全管理办法.docx"
    assert upload["timeout"] == 5
    assert submit["json"] == [{
        "org_data_id": "doc-7",
        "file_name": "数据安全管理办法.docx",
        "file_type": "docx",
        "file_size": len(b"docx-bytes"),
        "url": "http://iobs/file.docx",
        "text_labels": ["doc:数据安全管理办法"],
        "created_by": "clause_splitter",
    }]


def test_ingest_generates_org_id_when_missing(tmp_path):
    src = tmp_path / "制度.pdf"
    src.write_bytes(b"pdf")

    def handler(method, url, headers, kwargs):
        if url.endswith("/text_data_process/app/file"):
            return _upload_ok("http://iobs/file.pdf")
        return _ok(None)

    session = _Session(handler)
    info = _provider(session).ingest(src, "制度.pdf")

    uuid.UUID(info.doc_id)
    assert session.calls[1]["json"][0]["org_data_id"] == info.doc_id


def test_ingest_name_falls_back_to_in_process_map(tmp_path):
    src = tmp_path / "制度X.pdf"
    src.write_bytes(b"pdf")

    def handler(method, url, headers, kwargs):
        if url.endswith("/text_data_process/app/file"):
            return _upload_ok("http://iobs/file.pdf")
        if "/text_data_process/app/doc/" in url:
            return _ok("org-1")
        return _ok([{"id": "c9", "org_data_id": "org-1", "text": "第三条 丙", "score": 0.5}])

    provider = _provider(_Session(handler))
    provider.ingest(src, "制度X.pdf", "doc-hint")
    hits = provider.search("q", 1)
    assert hits[0].doc_id == "制度X"


def test_ingest_404_is_not_found(tmp_path):
    src = tmp_path / "制度.pdf"
    src.write_bytes(b"pdf")

    def handler(method, url, headers, kwargs):
        if url.endswith("/text_data_process/app/file"):
            return _upload_ok("http://iobs/file.pdf")
        return _Resp(404, {"status": "fail", "msg": "no route"})

    with pytest.raises(RagNotFound):
        _provider(_Session(handler)).ingest(src, "制度.pdf", "doc-1")


# ── delete ──────────────────────────────────────────────────────────────


def test_delete_sends_ids_in_one_batch():
    session = _Session(lambda *a: _Resp(204))
    assert _provider(session).delete("org-1") is True
    call = session.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/text_data_process/app/doc/batch/idx_1")
    assert call["json"] == ["org-1"]


def test_delete_404_counts_as_removed():
    session = _Session(lambda *a: _Resp(404, {"status": "fail", "msg": "gone"}))
    assert _provider(session).delete("org-1") is True


# ── envelope & status mapping ───────────────────────────────────────────


@pytest.mark.parametrize("body", [
    {"status": "fail", "msg": "boom", "data": None},
    {"msg": "no status", "data": []},
])
def test_bad_envelope_raises_response_error(body):
    with pytest.raises(RagResponseError):
        _provider(_Session(lambda *a: _Resp(200, body))).search("q", 1)


def test_non_json_response_raises_response_error():
    session = _Session(lambda *a: _Resp(200, raw="oops"))
    with pytest.raises(RagResponseError):
        _provider(session).search("q", 1)


@pytest.mark.parametrize("status,exc", [
    (401, RagAuthError),
    (403, RagAuthError),
    (408, RagUnavailable),
    (409, RagUnavailable),
    (429, RagUnavailable),
    (500, RagUnavailable),
    (400, RagResponseError),
])
def test_http_status_mapping(status, exc):
    session = _Session(lambda *a: _Resp(status, {"status": "fail", "msg": "x"}))
    with pytest.raises(exc):
        _provider(session).search("q", 1)


# ── concurrency ─────────────────────────────────────────────────────────


def test_concurrency_limit_is_enforced():
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def handler(*_):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return _ok([])

    provider = _provider(_Session(handler), max_concurrency=2)
    threads = [threading.Thread(target=provider.search, args=("q", 1)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["peak"] <= 2
