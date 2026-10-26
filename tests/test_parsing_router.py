"""External parse routing — remote result first, local text fallback."""

from __future__ import annotations

import pytest

from clause_splitter.parsing.router import parse_document
from tests.fakes import FakeRagService

LOCAL_TEXT = "第一条　本地抽取的条款。\n第二条　本地第二条。"


def _service(**attrs) -> FakeRagService:
    service = FakeRagService()
    for key, value in attrs.items():
        setattr(service, key, value)
    return service


def _source(tmp_path):
    src = tmp_path / "外规.txt"
    src.write_text(LOCAL_TEXT, encoding="utf-8")
    return src


def test_remote_text_is_preferred(tmp_path):
    service = _service(parse_text="第一条　远端条款。\n第二条　远端第二条。")
    parsed = parse_document(_source(tmp_path), "外规", service=service)
    assert parsed.source == "remote"
    assert [b.text for b in parsed.blocks] == ["第一条　远端条款。", "第二条　远端第二条。"]


def test_remote_failure_falls_back_to_local(tmp_path):
    parsed = parse_document(_source(tmp_path), "外规", service=_service())
    assert parsed.source == "local"
    assert [b.text for b in parsed.blocks] == LOCAL_TEXT.splitlines()


def test_empty_remote_response_falls_back_to_local(tmp_path):
    service = _service(parse_error=None)  # answers, but with no text
    parsed = parse_document(_source(tmp_path), "外规", service=service)
    assert parsed.source == "local"
    assert parsed.blocks


def test_fallback_disabled_surfaces_the_remote_error(tmp_path, monkeypatch):
    monkeypatch.setattr("clause_splitter.parsing.router.PARSE_FALLBACK", "none")
    with pytest.raises(ValueError, match="本地回退"):
        parse_document(_source(tmp_path), "外规", service=_service())
