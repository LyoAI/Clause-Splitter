"""Excel clause parsing tools — deterministic functions for ReAct Excel node.

These tools handle the mechanical parts of Excel reading. The LLM handles
the intelligent part (schema detection / column mapping). Once the mapping
is known, extraction is deterministic.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from utils import get_logger

logger = get_logger()

# ── Column header normalisation ────────────────────────────────────────────


def _normalize_column_header(name: str | None) -> str:
    """Normalize Excel column header for fuzzy matching.

    Strips whitespace, full-width/half-width brackets, special chars.
    "条款内容 " and "条款内容（原文）" both normalise to "条款内容".

    Args:
        name: Raw column header string (may be None).

    Returns:
        Normalised lower-case string for comparison.
    """
    if name is None:
        return ""
    text = str(name).strip()
    # Remove parenthetical annotations: （...）, (...), [...]
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    # Remove full-width/half-width brackets and special chars
    text = re.sub(r"[【】〈〉《》\s]+", "", text)
    return text.strip()


def _normalize_mapping(mapping: dict[str, str]) -> dict[str, str]:
    """Normalize all values in a column mapping dict for fuzzy matching."""
    return {k: _normalize_column_header(v) for k, v in mapping.items()}


# ── Article ID inference ───────────────────────────────────────────────────


def _infer_article_id_from_text(text: str) -> str:
    """Fallback: extract article ID from clause text using regex.

    Tries common Chinese legal numbering patterns:
    - "第X条"
    - "第X节"
    - "Article X"
    - Numbered items

    Args:
        text: Clause full text.

    Returns:
        Extracted article ID, or "未编号条款" if nothing found.
    """
    if not text or not text.strip():
        return "空白条款"

    # Priority patterns
    patterns = [
        r"第([一二三四五六七八九十百零\d]+)条",       # 第X条
        r"第([一二三四五六七八九十百零\d]+)节",       # 第X节
        r"第([一二三四五六七八九十百零\d]+)章",       # 第X章
        r"(Article\s+[IVXLCDM\d]+)",                    # Article X
        r"^(\d+[\.\、\)）]\s*)",                         # 1. / 1、 / 1)
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(0).strip()

    # Fallback: use first 30 chars as ID
    snippet = text.strip()[:30]
    return f"[{snippet}...]" if len(text) > 30 else snippet


# ── Excel preview (for LLM analysis) ───────────────────────────────────────


def read_excel_preview(
    file_path: str | Path,
    max_rows: int = 30,
) -> dict[str, Any]:
    """Read sample rows from an Excel file for LLM schema analysis.

    Reads the header row (assumed row 1) and up to max_rows data rows
    from the first sheet. Returns structured preview data that the LLM
    can use to determine column mapping.

    Args:
        file_path: Path to .xlsx/.xls file.
        max_rows: Maximum data rows to sample.

    Returns:
        {
            "file_path": str,
            "sheet_names": ["Sheet1", ...],
            "active_sheet": str,
            "total_rows": int,          # data rows (excl. header)
            "column_headers": {"A": "内部制度名称", "B": "条款内容", ...},
            "samples": [
                {"row": 2, "columns": {"A": "制度A", "B": "第一条 ...", ...}},
                ...
            ],
            "error": str | None,
        }
    """
    import openpyxl

    path = Path(file_path)
    result: dict[str, Any] = {
        "file_path": str(path),
        "sheet_names": [],
        "active_sheet": "",
        "total_rows": 0,
        "column_headers": {},
        "samples": [],
        "error": None,
    }

    if not path.exists():
        result["error"] = f"文件不存在: {file_path}"
        return result

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        result["error"] = f"无法打开 Excel 文件: {e}"
        return result

    result["sheet_names"] = wb.sheetnames

    if not wb.sheetnames:
        result["error"] = "Excel 文件中没有工作表"
        wb.close()
        return result

    # Use first sheet
    sheet_name = wb.sheetnames[0]
    result["active_sheet"] = sheet_name
    ws = wb[sheet_name]

    # ── Read header row ───────────────────────────────────────────────
    header_row = None
    col_letters = {}

    rows_iter = ws.iter_rows(min_row=1, max_row=1, values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        pass

    if header_row:
        for idx, val in enumerate(header_row):
            col_letter = _col_idx_to_letter(idx)
            col_letters[idx] = col_letter
            result["column_headers"][col_letter] = str(val).strip() if val is not None else ""

    # ── Read sample data rows ─────────────────────────────────────────
    sample_count = 0
    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if sample_count >= max_rows:
            break

        # Skip completely empty rows
        if all(v is None or str(v).strip() == "" for v in row):
            continue

        row_data: dict[str, str] = {}
        for col_idx, val in enumerate(row):
            col_letter = col_letters.get(col_idx, _col_idx_to_letter(col_idx))
            row_data[col_letter] = str(val).strip() if val is not None else ""

        if row_data:
            result["samples"].append({"row": row_idx, "columns": row_data})
            sample_count += 1

    # Count total data rows (approximate — read max 5000 for perf)
    total = 0
    for _ in ws.iter_rows(min_row=2, values_only=True):
        if any(v is not None and str(v).strip() != "" for v in _):
            total += 1
        if total >= 5000:
            break
    result["total_rows"] = total

    wb.close()
    logger.info(
        "Excel preview: %s [%s] — %d samples / ~%d total rows, %d headers",
        path.name, sheet_name, sample_count, total, len(result["column_headers"]),
    )
    return result


# ── Clause extraction ──────────────────────────────────────────────────────


def extract_excel_articles(
    file_path: str | Path,
    column_mapping: dict[str, str],
    sheet_name: str | None = None,
    max_rows: int = 5000,
) -> dict[str, Any]:
    """Extract all rows from Excel using LLM-detected column mapping.

    After the LLM identifies which column maps to which field, this function
    reads all rows and converts them to ArticleInfo-compatible dicts.

    Required keys in column_mapping:
        article_text: column for the full clause text (required)

    Optional keys in column_mapping:
        article_id: column for the clause/article number
        document_name: column for the regulation/document name
        section: column for the chapter/section
        additional_context: column with supplementary context

    Args:
        file_path: Path to .xlsx/.xls file.
        column_mapping: {"article_text": "条款内容", "article_id": "条款名称", ...}
        sheet_name: Sheet name (default: first sheet).
        max_rows: Maximum rows to extract.

    Returns:
        {
            "articles": [
                {
                    "article_id": "第一条",
                    "full_text": "完整条款文本...",
                    "document_name": "制度A",
                    "section": "第一章",
                    "row_number": 2,
                },
                ...
            ],
            "document_groups": {
                "制度A": [article_dict, ...],
                "制度B": [article_dict, ...],
            },
            "total_rows": 500,
            "unique_documents": ["制度A", "制度B"],
            "error": str | None,
        }
    """
    import openpyxl

    path = Path(file_path)

    result: dict[str, Any] = {
        "articles": [],
        "document_groups": {},
        "total_rows": 0,
        "unique_documents": [],
        "error": None,
    }

    if not path.exists():
        result["error"] = f"文件不存在: {file_path}"
        return result

    # Normalize the column mapping for fuzzy matching
    norm_mapping = _normalize_mapping(column_mapping)

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        result["error"] = f"无法打开 Excel 文件: {e}"
        return result

    # Resolve sheet
    target_sheet = sheet_name or wb.sheetnames[0]
    if target_sheet not in wb.sheetnames:
        result["error"] = f"工作表 '{target_sheet}' 不存在。可用工作表: {', '.join(wb.sheetnames)}"
        wb.close()
        return result

    ws = wb[target_sheet]

    # ── Read header row and build column index ─────────────────────────
    header_row = None
    try:
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    except StopIteration:
        pass

    if not header_row:
        result["error"] = "Excel 文件第一行为空（需要表头行）"
        wb.close()
        return result

    # Build header → column index map
    header_map: dict[str, int] = {}  # normalized_header → col_index
    for idx, val in enumerate(header_row):
        norm = _normalize_column_header(val)
        if norm:
            header_map[norm] = idx

    # Resolve column indices from mapping
    col_indices: dict[str, int] = {}  # field_name → col_index
    missing_fields: list[str] = []

    for field_name, norm_target in norm_mapping.items():
        if norm_target in header_map:
            col_indices[field_name] = header_map[norm_target]
        else:
            # Try fuzzy match (substring)
            found = False
            for norm_header, h_idx in header_map.items():
                if norm_target in norm_header or norm_header in norm_target:
                    col_indices[field_name] = h_idx
                    found = True
                    logger.debug(
                        "Fuzzy match: '%s' → header '%s' for field '%s'",
                        norm_target, norm_header, field_name,
                    )
                    break
            if not found:
                missing_fields.append(f"{field_name} (目标列: {column_mapping.get(field_name, '?')})")

    # ── Ensure article_text is resolved ──────────────────────────────
    if "article_text" not in col_indices:
        # Last resort: find the column with the most text content
        if missing_fields:
            result["error"] = (
                f"无法在 Excel 中找到以下字段对应的列: {', '.join(missing_fields)}。"
                f"可用的表头: {', '.join(header_map.keys())}"
            )
        else:
            result["error"] = "column_mapping 中缺少必需的 'article_text' 字段"
        wb.close()
        return result

    # ── Read data rows ─────────────────────────────────────────────────
    text_col = col_indices["article_text"]
    id_col = col_indices.get("article_id", -1)
    doc_col = col_indices.get("document_name", -1)
    section_col = col_indices.get("section", -1)
    context_col = col_indices.get("additional_context", -1)

    row_count = 0
    document_groups: dict[str, list[dict[str, Any]]] = {}

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row_count >= max_rows:
            logger.warning("Reached max_rows limit (%d) for Excel extraction", max_rows)
            break

        # Get article text
        if text_col >= len(row) or row[text_col] is None:
            continue
        article_text = str(row[text_col]).strip()
        if not article_text:
            continue

        # Get optional fields
        article_id = ""
        if id_col >= 0 and id_col < len(row) and row[id_col] is not None:
            article_id = str(row[id_col]).strip()

        document_name = ""
        if doc_col >= 0 and doc_col < len(row) and row[doc_col] is not None:
            document_name = str(row[doc_col]).strip()

        section = ""
        if section_col >= 0 and section_col < len(row) and row[section_col] is not None:
            section = str(row[section_col]).strip()

        # Build full text with context
        full_text_parts: list[str] = []

        if context_col >= 0 and context_col < len(row) and row[context_col] is not None:
            ctx = str(row[context_col]).strip()
            if ctx and ctx != article_text:
                full_text_parts.append(ctx)

        full_text_parts.append(article_text)
        full_text = "\n".join(full_text_parts)

        # Infer article_id if missing
        if not article_id:
            article_id = _infer_article_id_from_text(article_text)

        article_dict = {
            "article_id": article_id,
            "full_text": full_text,
            "document_name": document_name,
            "section": section,
            "row_number": row_idx,
            "source_sheet": target_sheet,
        }

        result["articles"].append(article_dict)
        row_count += 1

        # Group by document
        group_key = document_name or "_未命名文档_"
        if group_key not in document_groups:
            document_groups[group_key] = []
        document_groups[group_key].append(article_dict)

    wb.close()

    result["total_rows"] = row_count
    result["document_groups"] = document_groups
    result["unique_documents"] = sorted(document_groups.keys())

    logger.info(
        "Excel extraction: %d articles across %d documents from %s",
        row_count, len(document_groups), path.name,
    )

    return result


# ── Helpers ─────────────────────────────────────────────────────────────────


def _col_idx_to_letter(idx: int) -> str:
    """Convert 0-based column index to Excel column letter(s).

    0 → A, 1 → B, 25 → Z, 26 → AA, etc.
    """
    letters = ""
    n = idx
    while n >= 0:
        letters = chr(ord("A") + (n % 26)) + letters
        n = n // 26 - 1
    return letters
