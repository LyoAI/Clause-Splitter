"""Tests for the Excel multi-regulation container (``containers.split_workbook``).

An internal-regulation library is often one Excel where every row is an article
(第一条 / 第二条 …) and rows are grouped by a 制度名称 column. ``split_workbook``
splits such a workbook into per-regulation ``RegulationDoc`` block lists so the
rest of the ingest pipeline treats each 制度 as its own document, chunked per-条
with the 条号 as ``meta.section``.

Covers:
- Per-制度 split with deterministic (no-LLM) column mapping via header keywords.
- Chunking: one chunk per 条, ``meta.section`` = 条号.
- Blank 制度名称 rows → the ``_未命名文档_`` fallback doc.
- Unrecognizable workbook (no content column) → clear ``ValueError``.
- End-to-end via ``ingest``: the store is keyed by 制度名.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from clause_splitter.tools.parser import chunk_doc
from clause_splitter.tools.parser.containers import split_workbook
from clause_splitter.tools.parser.headings import infer_regulation_headings
from clause_splitter.tools.parser.ingest import ingest


def _write_workbook(path: Path, rows: list[dict]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "制度"
    ws.append(["制度名称", "条款名称", "条款内容"])
    for r in rows:
        ws.append([r.get("doc", ""), r.get("id", ""), r.get("text", "")])
    wb.save(path)


# ------------------------------------------------------------- splitting -----

def test_split_workbook_per_regulation_articles(tmp_path: Path) -> None:
    wb_path = tmp_path / "内规库.xlsx"
    _write_workbook(
        wb_path,
        [
            {"doc": "网络数据安全管理条例", "id": "第一条", "text": "为了规范网络数据处理活动。", },
            {"doc": "网络数据安全管理条例", "id": "第二条", "text": "适用对象为本公司。", },
            {"doc": "个人信息保护办法", "id": "第一条", "text": "个人信息处理应当遵循合法原则。", },
            {"doc": "个人信息保护办法", "id": "第二条", "text": "不得非法收集他人信息。", },
        ],
    )

    docs = split_workbook(wb_path)
    assert [d.doc_id for d in docs] == ["网络数据安全管理条例", "个人信息保护办法"]
    assert all(d.source_file == str(wb_path) for d in docs)

    # Each regulation → alternating 条号 title (level 1) + body paragraph blocks.
    first = docs[0]
    assert [(b.type, b.text) for b in first.blocks] == [
        ("title", "第一条"),
        ("paragraph", "为了规范网络数据处理活动。"),
        ("title", "第二条"),
        ("paragraph", "适用对象为本公司。"),
    ]


def test_container_blocks_chunk_per_article(tmp_path: Path) -> None:
    wb_path = tmp_path / "内规库.xlsx"
    _write_workbook(
        wb_path,
        [
            {"doc": "数据安全制度", "id": "第一条", "text": "第一条　数据分类分级。"},
            {"doc": "数据安全制度", "id": "第二条", "text": "数据由专人管理。"},
        ],
    )
    reg = split_workbook(wb_path)[0]
    chunks = chunk_doc(reg.doc_id, infer_regulation_headings(reg.blocks), min_tokens=0)
    assert [c.meta["section"] for c in chunks] == ["第一条", "第二条"]
    assert len(chunks) == 2
    # The cell already leads with "第一条　" — the label is not duplicated.
    assert chunks[0].text == "第一条\n\n数据分类分级。"


def test_blank_doc_name_falls_back_to_unnamed(tmp_path: Path) -> None:
    wb_path = tmp_path / "内规库.xlsx"
    _write_workbook(
        wb_path,
        [{"id": "第一条", "text": "没有制度名的条款。"}],
    )
    docs = split_workbook(wb_path)
    assert len(docs) == 1
    assert docs[0].doc_id == "_未命名文档_"
    assert docs[0].blocks[0].text == "第一条"


def test_unrecognizable_workbook_raises(tmp_path: Path) -> None:
    wb_path = tmp_path / "随机.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["姓名", "部门"])
    ws.append(["张三", "IT"])
    wb.save(wb_path)
    with pytest.raises(ValueError, match="无法识别"):
        split_workbook(wb_path)


def test_blank_article_id_inferred_from_text(tmp_path: Path) -> None:
    wb_path = tmp_path / "内规库.xlsx"
    _write_workbook(
        wb_path,
        [{"doc": "某制度", "text": "第三条　明确责任分工。"}],  # 条款名称 blank
    )
    reg = split_workbook(wb_path)[0]
    assert reg.blocks[0].text == "第三条"
    assert reg.blocks[1].text == "明确责任分工。"


# ------------------------------------------------------------- end-to-end ----

def test_ingest_routes_workbook_to_per_doc_store(tmp_path: Path) -> None:
    wb_path = tmp_path / "内规库.xlsx"
    _write_workbook(
        wb_path,
        [
            {"doc": "制度甲", "id": "第一条", "text": "甲第一条内容。"},
            {"doc": "制度乙", "id": "第一条", "text": "乙第一条内容。"},
        ],
    )
    store = ingest([wb_path], name="tmp", persist=False)
    assert set(store.docs) == {"制度甲", "制度乙"}
    chunks = store.all_chunks()
    assert [c.meta["section"] for c in chunks] == ["第一条", "第一条"]
    # Provenance: per-doc source entry, kind=excel_regulation, sha256 of the workbook.
    assert set(store.sources) == {"制度甲", "制度乙"}
    for entry in store.sources.values():
        assert entry["kind"] == "excel_regulation"
        assert entry["source_file"] == str(wb_path)
        assert len(entry["sha256"]) == 64
