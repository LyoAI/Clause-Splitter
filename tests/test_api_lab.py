"""api_lab batch upload — path expansion and the summary table (no HTTP)."""

from __future__ import annotations

import pytest

import scripts.api_lab as api_lab


def _touch(tmp_path, name: str):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


def test_expand_picks_documents_from_a_directory(tmp_path):
    for name in ("a.docx", "b.pdf", "c.txt", "notes.exe", ".DS_Store"):
        _touch(tmp_path, name)
    _touch(tmp_path, "sub/d.docx")

    flat = [p.name for p in api_lab._expand_upload_paths([str(tmp_path)], recursive=False)]
    assert flat == ["a.docx", "b.pdf", "c.txt"]  # sorted, dotfiles/exe skipped

    deep = [p.name for p in api_lab._expand_upload_paths([str(tmp_path)], recursive=True)]
    assert "d.docx" in deep


def test_expand_keeps_explicit_files_and_dedupes(tmp_path):
    doc = _touch(tmp_path, "a.docx")
    files = api_lab._expand_upload_paths([str(tmp_path), str(doc)], recursive=False)
    assert [p.name for p in files] == ["a.docx"]


def test_expand_rejects_a_missing_path():
    with pytest.raises(SystemExit):
        api_lab._expand_upload_paths(["/no/such/path"], recursive=False)


class _Args:
    def __init__(self, files, recursive=False, continue_on_error=False):
        self.file = [str(f) for f in files]
        self.recursive = recursive
        self.continue_on_error = continue_on_error


def test_batch_upload_summarizes_created_duplicate_and_failed(tmp_path, monkeypatch, capsys):
    files = [_touch(tmp_path, n) for n in ("a.docx", "b.docx", "c.docx")]
    bodies = {
        "a.docx": {"msg": "created", "data": {"id": "id-a", "remote_state": "indexed", "chunk_count": 3}},
        "b.docx": {"msg": "duplicate", "data": {"id": "id-b", "remote_state": "indexed", "chunk_count": 1}},
    }

    def _fake(client, path):
        if path.name == "c.docx":
            raise RuntimeError("HTTP 500: boom")
        return bodies[path.name]

    monkeypatch.setattr(api_lab, "_upload_one", _fake)
    with pytest.raises(SystemExit) as exc:
        api_lab.cmd_lib_upload(object(), _Args(files, continue_on_error=True))

    assert exc.value.code == 1  # nonzero exit so scripts can tell
    out = capsys.readouterr().out
    assert "新建 1 / 重复 1 / 失败 1" in out
    assert "c.docx: HTTP 500: boom" in out
    assert "id-a" in out and "indexed" in out


def test_batch_upload_stops_at_the_first_failure_without_the_flag(tmp_path, monkeypatch, capsys):
    files = [_touch(tmp_path, n) for n in ("a.docx", "b.docx", "c.docx")]
    seen: list[str] = []

    def _fake(client, path):
        seen.append(path.name)
        if path.name == "b.docx":
            raise RuntimeError("HTTP 400: bad file")
        return {"msg": "created", "data": {"id": f"id-{path.name}", "remote_state": "indexed",
                                           "chunk_count": 1}}

    monkeypatch.setattr(api_lab, "_upload_one", _fake)
    with pytest.raises(SystemExit):
        api_lab.cmd_lib_upload(object(), _Args(files))

    assert seen == ["a.docx", "b.docx"]  # c.docx never attempted
    out = capsys.readouterr().out
    assert "未执行 1" in out
