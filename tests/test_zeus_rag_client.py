"""Zeus search: configuration, request shape, row mapping and error mapping.

Offline: the HTTP session is replaced by a stub that records the request and
returns a canned response. Nothing here touches the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag.errors import (
    RagAuthError,
    RagNotConfigured,
    RagNotFound,
    RagResponseError,
    RagUnavailable,
)
from rag.providers.zeus import ZeusProvider, _raise_status, _unwrap


class _Session:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, timeout=None, json=None, **_):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        body = {"status": "success", "data": self.rows}
        return SimpleNamespace(status_code=200, content=b"x", text="", json=lambda: body)


def _provider(session: _Session, **kwargs) -> ZeusProvider:
    p = ZeusProvider("http://zeus", "app", "secret", "idx", **kwargs)
    p._session = session
    return p


def test_available_requires_every_credential():
    assert ZeusProvider("http://zeus", "app", "secret", "idx").available
    assert not ZeusProvider("", "app", "secret", "idx").available
    assert not ZeusProvider("http://zeus", "", "secret", "idx").available
    assert not ZeusProvider("http://zeus", "app", "", "idx").available
    assert not ZeusProvider("http://zeus", "app", "secret", "").available


def test_search_sends_headers_and_documented_body():
    session = _Session(rows=[])
    _provider(session, kb_limit=20).search("数据安全", 3, labels=["internal-regulation"])
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/server/standard_search")
    assert call["headers"]["x-app-id"] == "app"
    assert call["headers"]["x-index"] == "idx"
    kb = call["json"]["kb"][0]
    assert kb["alias"] == "idx"
    assert kb["filter"]["text_labels"] == ["internal-regulation"]
    assert kb["limit"] == 20
    assert call["json"]["limit"] == 3


def test_search_without_labels_sends_empty_filter_whole_index():
    session = _Session(rows=[])
    _provider(session).search("数据安全", 3)
    assert session.calls[0]["json"]["kb"][0]["filter"]["text_labels"] == []


def test_search_maps_rows_dedupes_by_best_score_and_doc_id_is_org_data_id():
    rows = [
        {"id": "c#1", "org_data_id": "DOC-9", "text": "第一条 甲", "score": 0.4,
         "text_labels": ["doc:办法", "internal-regulation"]},
        {"id": "c#1", "org_data_id": "DOC-9", "text": "第一条 甲", "score": 0.9},
        {"id": "c#2", "text": "第二条 乙", "score": 0.5, "text_labels": []},
        {"id": "x", "text": "非文档", "data_type": "image"},
    ]
    hits = _provider(_Session(rows=rows)).search("q", 10)
    assert [h.chunk_id for h in hits] == ["c#1", "c#2"]
    assert hits[0].score == 0.9
    # doc:<stem> labels are backend-specific: doc_id is org_data_id only.
    assert hits[0].doc_id == "DOC-9"
    assert hits[0].meta["org_data_id"] == "DOC-9"
    assert hits[1].doc_id == ""
    assert hits[0].section == "第一条 甲"


def test_search_404_means_search_not_enabled(monkeypatch):
    provider = _provider(_Session())

    def not_found(*_args, **_kwargs):
        raise RagNotFound("404", status_code=404)

    monkeypatch.setattr(provider, "_request", not_found)
    with pytest.raises(RagNotConfigured):
        provider.search("q", 3)


def test_http_status_mapping():
    def resp(code, text=""):
        return SimpleNamespace(status_code=code, text=text)

    with pytest.raises(RagAuthError):
        _raise_status(resp(401), "POST", "/x")
    with pytest.raises(RagNotFound):
        _raise_status(resp(404), "POST", "/x")
    with pytest.raises(RagUnavailable):
        _raise_status(resp(503), "POST", "/x")
    with pytest.raises(RagResponseError):
        _raise_status(resp(400, "bad"), "POST", "/x")


def test_bad_envelope_and_business_error_raise_response_error():
    with pytest.raises(RagResponseError):
        _unwrap(SimpleNamespace(status_code=200, content=b"{}", text="{}", json=lambda: {}))
    fail = {"status": "fail", "msg": "error"}
    with pytest.raises(RagResponseError, match="fail"):
        _unwrap(SimpleNamespace(status_code=200, content=b"x", text="{}", json=lambda: fail))
