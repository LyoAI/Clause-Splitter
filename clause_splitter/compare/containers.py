"""Excel multi-regulation container: one workbook → per-制度 block lists.

An internal-regulation library is often a single Excel where every row is one
article (第一条 / 第二条 …) and rows are grouped by a 制度名称 column — each
《制度》 is its own regulation with its own articles. ``split_workbook`` splits
such a workbook into per-regulation ``RegulationDoc`` block lists so the rest of
the ingest pipeline (heading pass + chunker) treats each regulation as its own
document: ``doc_id`` = 制度名, one chunk per 条 with the 条号 as ``meta.section``.

Column mapping is deterministic — no LLM: ``split.parse``'s heuristic keyword
mapping over the header row (条款内容 / 制度名称 / 条款名称 …), with a
longest-average-text-column fallback for the content column. Rows are grouped
by the 制度名称 column via ``extract_excel_articles``.

``.xls`` (legacy binary) is not supported by openpyxl — convert to ``.xlsx``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from clause_splitter.parsing.blocks import Block

from clause_splitter.split.parse import (
    _heuristic_column_detection,
    _infer_article_id_from_text,
    extract_excel_articles,
    read_excel_preview,
)
from clause_splitter.utils import get_logger

logger = get_logger()

# Group key used by split.parse when the 制度名称 cell is blank.
_UNNAMED_DOC = "_未命名文档_"


def _strip_doc_title_brackets(name: str) -> str:
    """Strip one outer pair of 《》 from a regulation name.

    Internal regulation names are often written 《XX办法》 directly; without
    this the doc_id would become the doubled 《《XX办法》》. Only the outermost
    pair is stripped.
    """
    name = name.strip()
    if len(name) > 2 and name.startswith("《") and name.endswith("》"):
        return name[1:-1].strip()
    return name


@dataclass
class RegulationDoc:
    """One regulation extracted from a container workbook."""

    doc_id: str  # 制度名; caller uniquifies on cross-source collision
    source_file: str  # the workbook path — provenance for sources.json
    blocks: list[Block] = field(default_factory=list)


def split_workbook(path: str | Path) -> list[RegulationDoc]:
    """Split one Excel workbook into per-regulation block lists.

    Returns one ``RegulationDoc`` per distinct 制度名称 value, in first-seen
    order. Raises ``FileNotFoundError`` / ``ValueError`` when the file cannot
    be read or no content column can be found, so ingest can surface a clear
    message instead of silently indexing nothing.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Excel 内规库不存在: {path}")

    preview = read_excel_preview(path)
    if preview.get("error"):
        raise ValueError(f"无法读取 Excel 内规库 {path.name}: {preview['error']}")

    mapping = _heuristic_column_detection(preview)
    if not mapping:
        headers = [h for h in preview.get("column_headers", {}).values() if h]
        raise ValueError(
            f"无法识别 {path.name} 的条款内容列(在 sheet「{preview.get('active_sheet', '?')}」"
            f"检测到的表头: {headers});"
            "请确认该表含「条款内容 / 制度名称」等列,或将 .xls 转为 .xlsx"
        )

    extracted = extract_excel_articles(
        path, mapping, sheet_name=preview.get("active_sheet"),
    )
    if extracted.get("error"):
        raise ValueError(f"Excel 内规库抽取失败 {path.name}: {extracted['error']}")

    groups = extracted["document_groups"]
    if not groups:
        logger.warning("Excel 内规库 %s 没有任何可抽取的行(条款内容列为空?)", path.name)
    if "document_name" not in mapping:
        logger.warning(
            "Excel 内规库 %s 未识别到「制度名称」列,整库将按一个制度处理(doc_id=制度名 或 _未命名文档_)",
            path.name,
        )

    docs: list[RegulationDoc] = []
    for doc_name, articles in groups.items():
        doc_id = _strip_doc_title_brackets(doc_name) or _UNNAMED_DOC
        blocks: list[Block] = []
        order = 0
        for art in articles:
            full_text = (art.get("full_text") or "").strip()
            if not full_text:
                continue
            label = (art.get("article_id") or "").strip() or _infer_article_id_from_text(full_text)
            # Many cells already lead with the 条号 ("第一条　…"); strip it so
            # the chunk text is not "第一条\n\n第一条　…".
            body = full_text
            if label and body.startswith(label):
                body = body[len(label):].lstrip("　:：、.。 ").rstrip()
            # One title (条号, level 1 → opens its own chunk) + one body block,
            # in row order. The heading pass later leaves these untouched: the
            # marker-matched title re-splits into the same label + empty body.
            blocks.append(Block(f"{doc_id}#{order}h", "title", label, 1, order, 1))
            order += 1
            if body:
                blocks.append(Block(f"{doc_id}#{order}b", "paragraph", body, 1, order))
                order += 1
        if not blocks:
            logger.warning("制度《%s》在 %s 中没有可索引的条款行", doc_id, path.name)
            continue
        docs.append(RegulationDoc(doc_id=doc_id, source_file=str(path), blocks=blocks))
    return docs
