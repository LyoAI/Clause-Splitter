"""ChillRAG provider mapping — wire behaviour with a stub HTTP session."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clause_splitter.rag.errors import RagResponseError
from clause_splitter.rag.providers.chillrag import ChillRagProvider

INTERNAL = "clause_splitter_internal"
PARSE = "clause_splitter_parse"


class _Resp:
    def __init__(self, status: int = 200, data=None) -> None:
        self.status_code = status
        self._data = data
        self.content = b"x" if data is not None else b""
        self.text = json.dumps(data, ensure_ascii=False) if data is not None else ""

    def json(self):
        return self._data


class _Session:
    """Stub requests.Session that routes by path."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []
        self.logins = 0

    def post(self, url, json=None, timeout=None):
        self.logins += 1
        return _Resp(200, {"code": 0, "msg": "ok", "data": {"token": "t"}})

    def request(self, method, url, *, timeout=None, params=None, json=None, files=None, **_):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        self._files = files
        return self.handler(method, url, params, json)

    @property
    def files_name(self) -> str:
        return self._files[0][1][0]


def _ok(data):
    return _Resp(200, {"code": 0, "msg": "success", "data": data})


def _provider(session: _Session) -> ChillRagProvider:
    provider = ChillRagProvider(
        "http://rag.local", "u", "p",
        timeout_s=1, ingest_timeout_s=5, corpus=INTERNAL, parse_corpus=PARSE,
    )
    provider._session = session
    return provider


def _resources(*folders: str):
    return _ok([{"sourceId": i + 1, "resourceName": f} for i, f in enumerate(folders)])


def test_login_once_and_relogin_on_401():
    state = {"recall": 0}

    def handler(method, url, params, body):
        if url.endswith("/asset-file/resource"):
            return _resources(INTERNAL)
        state["recall"] += 1
        if state["recall"] == 1:
            return _Resp(401, {"code": 401, "msg": "expired", "data": None})
        return _ok([{
            "source": "77#0", "segment": "第一条　应当建立制度。",
            "score": 0.8, "metadata": "文件名称:数据安全管理办法",
        }])

    session = _Session(handler)
    hits = _provider(session).search("制度", 5)

    assert session.logins == 2  # initial login + one re-login after 401
    assert [h.chunk_id for h in hits] == ["77#0"]
    assert hits[0].doc_id == "数据安全管理办法"
    assert hits[0].section == "第一条　应当建立制度。"


def test_parse_joins_segments_in_index_order(tmp_path):
    uploaded: dict[str, str] = {}

    def handler(method, url, params, body):
        if url.endswith("/asset-file/resource"):
            return _resources(PARSE)
        if "web-upload" in url:
            name = session.files_name
            uploaded["name"] = name
            return _ok([12])
        if "upload/status" in url:
            return _ok({
                "state": "finish",
                "documents": 1, "chunks": 2,
                "files": [{"name": uploaded["name"], "fileId": 12, "chunks": 2}],
            })
        if "segments/12" in url:
            return _ok([
                {"segment": {"index": 1, "content": json.dumps({"content": "第二条 乙"})}},
                {"segment": {"index": 0, "content": json.dumps({"content": "第一条 甲"})}},
            ])
        raise AssertionError(url)

    session = _Session(handler)
    src = tmp_path / "外规.docx"
    src.write_bytes(b"docx")

    parsed = _provider(session).parse(src, "外规")

    assert parsed.text == "第一条 甲\n\n第二条 乙"
    assert parsed.doc_id == "外规"
    assert uploaded["name"].endswith(".docx")


def test_parse_rejects_unsupported_suffix_before_http(tmp_path):
    session = _Session(lambda *a: pytest.fail("no HTTP expected"))
    src = tmp_path / "外规.txt"
    src.write_text("第一条", encoding="utf-8")

    with pytest.raises(RagResponseError):
        _provider(session).parse(src, "外规")


def test_search_maps_dedupes_and_truncates():
    def handler(method, url, params, body):
        if url.endswith("/asset-file/resource"):
            return _resources(INTERNAL)
        return _ok([
            {"source": "11#0", "segment": "第一条 甲", "score": 0.4, "metadata": "文件名称:办法A"},
            {"source": "11#0", "segment": "第一条 甲", "score": 0.9, "metadata": "文件名称:办法A"},
            {"source": "12#1", "segment": "第二条 乙", "score": 0.7, "metadata": "文件名称:办法B"},
        ])

    hits = _provider(_Session(handler)).search("甲", 2)

    assert [(h.chunk_id, h.score) for h in hits] == [("11#0", 0.9), ("12#1", 0.7)]
    assert hits[0].doc_id == "办法A"
    assert hits[1].doc_id == "办法B"
