"""文档解析 —— 文本抽取 + Excel 条款解析.

- ``parse_document``:PDF/DOCX/DOC/TXT 抽文本;XLSX/XLS 标记 ``file_type="excel"``
  交由 ``parse_excel``。
- ``parse_excel``:LLM 列映射检测(ReAct 重试 ≤ excel_max_react_iterations)
  → 启发式回退 → 确定性抽取全部条款 → 构建 DocumentStructure + 合成 raw_text。

Excel 相关的确定性函数(预览 / 抽取 / 表头归一)一并放本模块。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from clause_splitter.config import get_settings
from clause_splitter.models import ArticleInfo, ComplexityLabel, DocumentStructure
from clause_splitter.split import prompts
from clause_splitter.split.structure import estimate_complexity
from clause_splitter.utils import get_logger
from clause_splitter.utils.file.doc_parser import extract_text

logger = get_logger()


# ═══════════════════════════════════════════════════════════════════════
# 文本文档解析
# ═══════════════════════════════════════════════════════════════════════


def parse_document(file_path: str | Path, document_name: str = "") -> dict[str, Any]:
    """解析文档,返回 {"raw_text", "file_type", "status", "error_message", "current_step"}。

    Excel 文件仅标记 file_type="excel",文本为空,由 parse_excel 继续处理。
    """
    path = Path(file_path)
    settings = get_settings()
    suffix = path.suffix.lower()
    logger.info("正在解析文档: %s (%s) [%s]", document_name, file_path, suffix)

    if not path.exists():
        return {
            "raw_text": "", "file_type": "",
            "status": "failed", "error_message": f"文件不存在: {file_path}",
            "current_step": "文档解析失败 —— 文件不存在",
        }

    if suffix in (".xlsx", ".xls"):
        logger.info("检测到 Excel 文件，路由至 Excel 解析: %s", document_name)
        return {
            "raw_text": "", "file_type": "excel",
            "progress": settings.progress_milestones["parse_complete"],
            "current_step": "Excel 文件已识别，准备结构化解析……",
            "status": "parsing",
        }

    try:
        raw_text = extract_text(path, max_chars=settings.max_chars_per_document)
    except Exception as e:
        logger.error("文档解析失败: %s", e)
        return {
            "raw_text": "", "file_type": "text",
            "status": "failed", "error_message": f"文档解析失败: {e}",
            "current_step": "文档解析失败",
        }

    if not raw_text or len(raw_text.strip()) < settings.min_chars_for_analysis:
        return {
            "raw_text": raw_text, "file_type": "text",
            "status": "failed", "error_message": "文档内容为空或过短，无法分析",
            "current_step": "文档解析失败 —— 内容为空",
        }

    logger.info("文档解析完成，共 %d 字符", len(raw_text))
    return {
        "raw_text": raw_text, "file_type": "text",
        "progress": settings.progress_milestones["parse_complete"],
        "current_step": "文档解析完成",
        "status": "parsing",
    }


# ═══════════════════════════════════════════════════════════════════════
# Excel 条款解析(LLM 列映射 + 启发式回退 + 确定性抽取)
# ═══════════════════════════════════════════════════════════════════════


def parse_excel(
    file_path: str | Path,
    document_name: str = "",
    llm: Any = None,
) -> dict[str, Any]:
    """解析 Excel 为结构化条款。

    Returns:
        {
            "document_structure": DocumentStructure,
            "raw_text": 合成文本(多文档时按 ## 分组),
            "total_articles": int,
            "excel_document_groups": [{"document_name", "article_count"}, ...],
            "document_groups": {doc_key: [article_dict, ...]},  # 原始分组(run_excel 用)
            "format_info": dict | None,
            "status": "ok" | "failed",
            "error_message": str,
        }
    """
    path = Path(file_path)
    settings = get_settings()

    if not path.exists():
        return _fail(f"文件不存在: {file_path}")

    logger.info("Excel 解析开始: %s (%s)", document_name, file_path)

    # ── Step 1: 读预览 ───────────────────────────────────────────────
    preview = read_excel_preview(path, max_rows=settings.excel_max_sample_rows)
    if preview.get("error"):
        return _fail(f"Excel 预览读取失败: {preview['error']}")

    samples = preview.get("samples", [])
    total_rows = preview.get("total_rows", 0)
    if not samples:
        return _fail("Excel 文件中没有数据行（仅表头或完全为空）")

    if total_rows > settings.max_excel_rows_per_document:
        logger.warning("Excel 行数过多 (%d)，超过上限 (%d)，将被截断", total_rows, settings.max_excel_rows_per_document)

    # ── Step 2: LLM schema 检测(ReAct 循环) ─────────────────────────
    column_mapping: dict[str, str] | None = None
    format_info: dict[str, Any] | None = None

    if llm is not None:
        column_mapping, format_info = _detect_column_mapping(preview, path, llm, settings)

    # ── Step 2b: 启发式回退 ─────────────────────────────────────────
    if column_mapping is None:
        logger.warning("LLM schema 检测失败，使用启发式回退方案")
        column_mapping = _heuristic_column_detection(preview)
        format_info = {
            "format_type": "未知（启发式检测）",
            "confidence": "low",
            "reasoning": "LLM 检测失败，使用正则/关键词匹配表头",
        }

    if column_mapping is None or not column_mapping.get("article_text"):
        return _fail(
            "无法识别 Excel 的列结构。请确保表格包含条款内容列（如'条款内容'、"
            "'法规内容'等；表头行和所在 sheet 会自动定位）。检测到的表头: "
            f"{list(preview.get('column_headers', {}).values())}"
        )

    # ── Step 3: 确定性抽取 ──────────────────────────────────────────
    extraction = extract_excel_articles(
        file_path=path,
        column_mapping=column_mapping,
        sheet_name=preview.get("active_sheet"),
        max_rows=settings.max_excel_rows_per_document,
    )
    if extraction.get("error"):
        return _fail(f"Excel 条款提取失败: {extraction['error']}")

    articles_data = extraction.get("articles", [])
    doc_groups = extraction.get("document_groups", {})
    unique_docs = extraction.get("unique_documents", [])

    if not articles_data:
        return _fail("从 Excel 中未提取到任何有效条款。请检查文件内容。")

    # ── Step 4: 转 ArticleInfo ───────────────────────────────────────
    articles: list[ArticleInfo] = []
    for ad in articles_data:
        text = ad.get("full_text", "")
        art_id = ad.get("article_id", "")
        section = ad.get("section", "")
        doc_name = ad.get("document_name", "")

        if not art_id:
            art_id = _infer_article_id_from_text(text)

        complexity = estimate_complexity(text)
        parent_parts = [p for p in [doc_name, section] if p]
        parent_section = " › ".join(parent_parts) if parent_parts else None

        articles.append(ArticleInfo(
            article_id=art_id,
            full_text=text,
            parent_section=parent_section,
            complexity_score=complexity["complexity_score"],
            complexity_label=(
                ComplexityLabel.COMPLEX
                if complexity["complexity_score"] >= settings.complex_solo_threshold
                else ComplexityLabel.SIMPLE
            ),
            estimated_obligation_count=max(1, complexity["metrics"]["sentence_count"]),
        ))

    # ── Step 5: 构建 DocumentStructure + 合成 raw_text ───────────────
    doc_title = document_name or path.stem
    doc_structure = DocumentStructure(
        document_title=doc_title,
        articles=articles,
        total_character_count=sum(len(a.full_text) for a in articles),
        structure_type="excel_structured",
    )

    raw_text_parts: list[str] = []
    for doc_key in unique_docs:
        group_articles = doc_groups.get(doc_key, [])
        if len(unique_docs) > 1:
            raw_text_parts.append(f"## {doc_key}")
        for art in group_articles:
            art_id = art.get("article_id", "")
            art_text = art.get("full_text", "")
            raw_text_parts.append(f"{art_id} {art_text}" if art_id else art_text)
        raw_text_parts.append("")
    synthetic_raw_text = "\n".join(raw_text_parts)

    excel_document_groups = [
        {"document_name": doc_key, "article_count": len(group_articles)}
        for doc_key, group_articles in doc_groups.items()
    ]

    logger.info(
        "Excel 解析完成: %d 条款 across %d 文档, %d chars, format=%s",
        len(articles), len(doc_groups), len(synthetic_raw_text),
        format_info.get("format_type", "?") if format_info else "?",
    )

    return {
        "document_structure": doc_structure,
        "raw_text": synthetic_raw_text,
        "total_articles": len(articles),
        "excel_document_groups": excel_document_groups,
        "document_groups": doc_groups,
        "format_info": format_info,
        "status": "ok",
        "error_message": "",
    }


def _fail(error_message: str) -> dict[str, Any]:
    return {
        "document_structure": None,
        "raw_text": "",
        "total_articles": 0,
        "excel_document_groups": [],
        "document_groups": {},
        "format_info": None,
        "status": "failed",
        "error_message": error_message,
    }


# ── LLM 列映射检测 ─────────────────────────────────────────────────────


def _detect_column_mapping(
    preview: dict,
    path: Path,
    llm: Any,
    settings,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    sample_text = prompts.format_samples_for_llm(preview)
    user_prompt = prompts.format_excel_user_prompt(
        sample_rows_text=sample_text,
        sheet_name=preview.get("active_sheet", ""),
        total_rows=preview.get("total_rows", 0),
        file_name=path.name,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.EXCEL_PARSE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    for iteration in range(settings.excel_max_react_iterations):
        try:
            response = llm.invoke(messages)
            content = response.content if hasattr(response, "content") else str(response)

            parsed = _parse_llm_excel_output(content)
            if parsed and "column_mapping" in parsed:
                cm = parsed["column_mapping"]
                if cm.get("article_text"):
                    logger.info(
                        "Excel schema 检测成功 (iter %d/%d): format=%s, confidence=%s, "
                        "mapping=%s, multi_doc=%s",
                        iteration + 1, settings.excel_max_react_iterations,
                        parsed.get("format_type", "?"), parsed.get("confidence", "?"),
                        {k: v for k, v in cm.items() if v},
                        parsed.get("is_multi_document", False),
                    )
                    return cm, parsed

            logger.warning("Excel schema 检测 iter %d: 输出无法解析或缺少 article_text", iteration + 1)
            messages.append({"role": "assistant", "content": content[:1000]})
            messages.append({"role": "user", "content": prompts.format_retry_feedback("column_mapping 缺失或 article_text 字段为空")})
        except Exception as e:
            logger.error("Excel schema 检测 iter %d 异常: %s", iteration + 1, e)
            if iteration < settings.excel_max_react_iterations - 1:
                messages.append({"role": "user", "content": prompts.format_retry_feedback(str(e))})

    return None, None


def _parse_llm_excel_output(content: str) -> dict | None:
    """解析 LLM 的 Excel 列映射 JSON(代码块 / 裸 JSON / 首个 {} 块)。"""
    if not content or not content.strip():
        return None

    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", content)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(content.strip())
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{[\s\S]+\}", content)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    return None


# ── 启发式列检测 ───────────────────────────────────────────────────────

# 表头关键词族(模块级,供启发式列映射与表头行定位共用)。
# 公司内网模板的表头五花八门(「内部制度名称」「制度条款内容」「条文」…),
# 关键词用"包含"匹配,归一化去掉括号注释/空白后再比。
_ARTICLE_TEXT_KEYWORDS = [
    "条款内容", "法规条款", "制度条款", "条文内容", "具体条款", "条款正文",
    "clause", "article_text", "content",
]
_ARTICLE_ID_KEYWORDS = [
    "条款名称", "条款编号", "条款标题", "条目名称", "条文号",
    "clause_name", "article_id", "title",
]
_DOC_NAME_KEYWORDS = [
    "内部制度名称", "制度名称", "外规名称", "法规名称", "文档名称", "文件名称",
    "document_name", "regulation_name",
]
_SECTION_KEYWORDS = ["章节", "所属章节", "章", "节", "section", "chapter"]
# 整篇制度全文列:内网模板常见的「制度内容」列,每行重复同一篇制度的完整内容。
# 这种列不能进 chunk —— 否则每个检索单元都≈整篇制度,检索失去区分度;也不能当
# additional_context(会被拼到每一行条款前面)。检测到即整列跳过。
_FULL_TEXT_KEYWORDS = [
    "制度内容", "制度全文", "全文内容", "完整内容", "整篇内容",
    "full_document",
]
# 真正"每行一条的补充上下文"列(适用条件 / 备注 等),会拼在条款内容之前。
_CONTEXT_KEYWORDS = [
    "适用条件", "适用范围", "适用对象", "备注",
    "document_context", "context",
]
_HEADER_KEYWORDS = (
    *_ARTICLE_TEXT_KEYWORDS,
    *_ARTICLE_ID_KEYWORDS,
    *_DOC_NAME_KEYWORDS,
    *_SECTION_KEYWORDS,
    *_FULL_TEXT_KEYWORDS,
    *_CONTEXT_KEYWORDS,
)


def _header_row_hits(cells: list[Any]) -> tuple[int, int]:
    """(表头关键词命中数, 非空去重单元格数) —— 用于定位真实表头行。"""
    kw_hits = 0
    seen: set[str] = set()
    for val in cells:
        if val is None:
            continue
        norm = _normalize_column_header(str(val))
        if not norm:
            continue
        seen.add(norm)
        low = norm.lower()
        if any(kw.lower() in low for kw in _HEADER_KEYWORDS):
            kw_hits += 1
    return kw_hits, len(seen)


def _find_header_row(ws, max_scan: int = 5) -> int:
    """返回真实表头行(1-based)。

    公司内网模板常在第 1 行前有若干合并标题/格式行(旧 split 模板
    ``header_rows=4 / data_start_row=5``)。逐行扫描前 ``max_scan`` 行,
    取"表头关键词命中数最多、非空去重单元格最多"的行作为表头;完全找不到
    关键词(裸表)时回退到行 1。
    """
    best_row = 1
    best_score = (-1, -1)
    for idx in range(1, max_scan + 1):
        values = next(ws.iter_rows(min_row=idx, max_row=idx, values_only=True), None)
        if values is None:
            break
        kw_hits, non_empty = _header_row_hits(values)
        score = (kw_hits, non_empty)
        if score > best_score:
            best_score = score
            best_row = idx
    # 整个扫描区间都没有"表头样"的行(单个合并标题也算不上表头)→ 回退行 1。
    if best_score == (0, 1) or best_score == (0, 0):
        return 1
    return best_row


def _match(col_name: str, keywords: list[str]) -> bool:
    """表头名(任意大小写/空格/括注)是否包含任一关键词。"""
    norm = _normalize_column_header(col_name).lower()
    return any(kw.lower() in norm for kw in keywords)


def _heuristic_column_detection(preview: dict) -> dict[str, str] | None:
    """无 LLM 时按关键词匹配表头(带内容兜底 + 制度名内容推断)。"""
    headers = preview.get("column_headers", {})
    if not headers:
        return None

    mapping: dict[str, str] = {}
    for col_letter, col_name in sorted(headers.items()):
        if not col_name:
            continue
        if _match(col_name, _FULL_TEXT_KEYWORDS):
            # 整篇制度全文列(每行同一篇完整内容)→ 整列跳过,不进 chunk。
            continue
        if not mapping.get("article_text") and _match(col_name, _ARTICLE_TEXT_KEYWORDS):
            mapping["article_text"] = col_name
        elif not mapping.get("article_id") and _match(col_name, _ARTICLE_ID_KEYWORDS):
            mapping["article_id"] = col_name
        elif not mapping.get("document_name") and _match(col_name, _DOC_NAME_KEYWORDS):
            mapping["document_name"] = col_name
        elif not mapping.get("section") and _match(col_name, _SECTION_KEYWORDS):
            mapping["section"] = col_name
        elif not mapping.get("additional_context") and _match(col_name, _CONTEXT_KEYWORDS):
            mapping["additional_context"] = col_name

    if mapping.get("article_text"):
        # 有内容列但没匹配到制度名列 → 用"重复短值"从内容推断(常见:表头叫
        # 「归属制度」「所在制度」等关键词没覆盖到的名字)。
        if not mapping.get("document_name"):
            doc_col = _find_document_column_by_content(
                preview, exclude={_col_name_to_letter(headers, mapping["article_text"])}
            )
            if doc_col:
                mapping["document_name"] = doc_col
                logger.info("制度名列按内容推断: '%s' → document_name", doc_col)
        logger.info(
            "启发式列检测: article_text='%s', mapping=%s",
            mapping["article_text"], {k: v for k, v in mapping.items() if k != "article_text"},
        )
        return mapping

    return _find_text_column_by_content(preview)


def _col_name_to_letter(headers: dict[str, str], col_name: str) -> str:
    """表头名 → 列字母(反向查找)。"""
    for letter, name in headers.items():
        if name == col_name:
            return letter
    return ""


def _find_text_column_by_content(preview: dict) -> dict[str, str] | None:
    """兜底 1:平均文本长度最长的列(>20 字符)当作条款内容列。"""
    samples = preview.get("samples", [])
    headers = preview.get("column_headers", {})
    if not samples or not headers:
        return None

    col_lengths: dict[str, list[int]] = {}
    for s in samples:
        for col_letter, value in s.get("columns", {}).items():
            col_lengths.setdefault(col_letter, []).append(len(value))

    best_col = None
    best_avg = 0
    for col_letter, lengths in col_lengths.items():
        if _match(headers.get(col_letter, ""), _FULL_TEXT_KEYWORDS):
            # 整篇制度全文列(每行同一篇完整内容)→ 兜底也不选它当条款内容。
            continue
        avg = sum(lengths) / len(lengths) if lengths else 0
        if avg > best_avg and avg > 20:
            best_avg = avg
            best_col = col_letter

    if best_col and best_col in headers:
        mapping: dict[str, str] = {"article_text": headers[best_col]}
        doc_col = _find_document_column_by_content(preview, exclude={best_col})
        if doc_col:
            mapping["document_name"] = doc_col
            logger.info("制度名列按内容推断: '%s' → document_name", doc_col)
        logger.info(
            "内容分析回退: 最长平均文本列 '%s' (%s) → article_text (avg %d chars)",
            best_col, headers[best_col], int(best_avg),
        )
        return mapping
    return None


def _find_document_column_by_content(
    preview: dict, exclude: set[str] | None = None,
) -> str | None:
    """兜底 2:从内容推断"制度名"列 —— 短值、且在多行重复出现的列。

    关键词匹配不到(表头叫「归属制度」「所在制度」等)时,制度名列的特征是:
    值都很短(制度名 <40 字)、同一个制度名对应多行条款(重复率高),而条款内容
    列基本不重复。取 (重复率 × 短值权重) 最高的未用列。
    """
    samples = preview.get("samples", [])
    headers = preview.get("column_headers", {})
    if not samples or not headers:
        return None
    exclude = exclude or set()

    stats: dict[str, dict] = {}
    for s in samples:
        for col_letter, value in s.get("columns", {}).items():
            if not value or col_letter in exclude:
                continue
            st = stats.setdefault(col_letter, {"rows": 0, "vals": set(), "max_len": 0})
            st["rows"] += 1
            st["vals"].add(value)
            st["max_len"] = max(st["max_len"], len(value))

    best_letter, best_score = None, 0.0
    for col_letter, st in stats.items():
        if st["rows"] < 3 or len(st["vals"]) <= 1:
            continue
        repetition = st["rows"] / len(st["vals"])
        length_weight = 1.0 if 2 <= st["max_len"] <= 40 else 0.1
        score = repetition * length_weight
        if score > best_score:
            best_score, best_letter = score, col_letter

    if best_letter and best_letter in headers:
        return headers[best_letter]
    return None


# ═══════════════════════════════════════════════════════════════════════
# Excel 确定性工具(预览 / 抽取 / 表头归一 / 条款号推断)
# ═══════════════════════════════════════════════════════════════════════


def _normalize_column_header(name: str | None) -> str:
    """归一化表头:去括号注释、特殊字符、空白。"条款内容（原文）" → "条款内容"。"""
    if name is None:
        return ""
    text = str(name).strip()
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    text = re.sub(r"[【】〈〉《》\s]+", "", text)
    return text.strip()


def _normalize_mapping(mapping: dict[str, str]) -> dict[str, str]:
    return {k: _normalize_column_header(v) for k, v in mapping.items()}


def _infer_article_id_from_text(text: str) -> str:
    """从条款文本推断条款号(第X条/第X节/第X章/Article X/编号项)。"""
    if not text or not text.strip():
        return "空白条款"

    patterns = [
        r"第([一二三四五六七八九十百零\d]+)条",
        r"第([一二三四五六七八九十百零\d]+)节",
        r"第([一二三四五六七八九十百零\d]+)章",
        r"(Article\s+[IVXLCDM\d]+)",
        r"^(\d+[\.\、\)）]\s*)",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(0).strip()

    snippet = text.strip()[:30]
    return f"[{snippet}...]" if len(text) > 30 else snippet


def read_excel_preview(file_path: str | Path, max_rows: int = 30) -> dict[str, Any]:
    """读 Excel 表头 + 样本行,供 LLM / 启发式分析列结构。

    自动处理两类内网模板的常见错位:
    - 表头不在第 1 行(前面有合并标题/格式行)→ ``_find_header_row`` 定位真实表头行;
    - 数据不在第一个 sheet(首 sheet 是封面/说明)→ 逐 sheet 比较表头关键词命中数,
      选最像数据表的 sheet,记入 ``active_sheet`` 供抽取阶段复用。
    """
    import openpyxl

    path = Path(file_path)
    result: dict[str, Any] = {
        "file_path": str(path), "sheet_names": [], "active_sheet": "",
        "total_rows": 0, "column_headers": {}, "samples": [], "error": None,
    }

    if not path.exists():
        result["error"] = f"文件不存在: {file_path}"
        return result

    try:
        # 系统导出的 xlsx 常带不完整的 <dimension> 元数据,read_only=True 流式只吐
        # A1 一格(真实表头/数据全读不到);普通模式物化全部行才可靠。
        wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
    except Exception as e:
        result["error"] = f"无法打开 Excel 文件: {e}"
        return result

    result["sheet_names"] = wb.sheetnames
    if not wb.sheetnames:
        result["error"] = "Excel 文件中没有工作表"
        wb.close()
        return result

    # ── 选数据 sheet:表头关键词命中最多者;全都不像则取第一个 sheet ──
    best_sheet = wb.sheetnames[0]
    best_score = (-1, -1)
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        hr = _find_header_row(ws)
        header_values = next(ws.iter_rows(min_row=hr, max_row=hr, values_only=True), ())
        score = _header_row_hits(header_values)
        if score > best_score:
            best_sheet, best_score = sheet, score

    result["active_sheet"] = best_sheet
    ws = wb[best_sheet]

    header_row_idx = _find_header_row(ws)
    header_row = next(
        ws.iter_rows(min_row=header_row_idx, max_row=header_row_idx, values_only=True), None
    )
    col_letters: dict[int, str] = {}
    if header_row:
        for idx, val in enumerate(header_row):
            col_letters[idx] = _col_idx_to_letter(idx)
            result["column_headers"][col_letters[idx]] = str(val).strip() if val is not None else ""

    sample_count = 0
    data_start = header_row_idx + 1
    for row_idx, row in enumerate(
        ws.iter_rows(min_row=data_start, values_only=True), start=data_start
    ):
        if sample_count >= max_rows:
            break
        if all(v is None or str(v).strip() == "" for v in row):
            continue
        row_data = {
            col_letters.get(col_idx, _col_idx_to_letter(col_idx)): (str(val).strip() if val is not None else "")
            for col_idx, val in enumerate(row)
        }
        result["samples"].append({"row": row_idx, "columns": row_data})
        sample_count += 1

    total = 0
    for row in ws.iter_rows(min_row=data_start, values_only=True):
        if any(v is not None and str(v).strip() != "" for v in row):
            total += 1
        if total >= 5000:
            break
    result["total_rows"] = total

    wb.close()
    logger.info(
        "Excel preview: %s [%s] header_row=%d — %d samples / ~%d total rows, %d headers",
        path.name, best_sheet, header_row_idx, sample_count, total, len(result["column_headers"]),
    )
    return result


def extract_excel_articles(
    file_path: str | Path,
    column_mapping: dict[str, str],
    sheet_name: str | None = None,
    max_rows: int = 5000,
) -> dict[str, Any]:
    """按列映射抽取 Excel 全部行,按 document_name 分组。"""
    import openpyxl

    path = Path(file_path)
    result: dict[str, Any] = {
        "articles": [], "document_groups": {}, "total_rows": 0,
        "unique_documents": [], "error": None,
    }

    if not path.exists():
        result["error"] = f"文件不存在: {file_path}"
        return result

    norm_mapping = _normalize_mapping(column_mapping)

    try:
        # 系统导出的 xlsx 常带不完整的 <dimension> 元数据,read_only=True 流式只吐
        # A1 一格(真实表头/数据全读不到);普通模式物化全部行才可靠。
        wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
    except Exception as e:
        result["error"] = f"无法打开 Excel 文件: {e}"
        return result

    target_sheet = sheet_name or wb.sheetnames[0]
    if target_sheet not in wb.sheetnames:
        result["error"] = f"工作表 '{target_sheet}' 不存在。可用工作表: {', '.join(wb.sheetnames)}"
        wb.close()
        return result
    ws = wb[target_sheet]

    # 与 preview 同一套表头行定位:前面可能有合并标题/格式行。
    header_row_idx = _find_header_row(ws)
    header_row = next(
        ws.iter_rows(min_row=header_row_idx, max_row=header_row_idx, values_only=True), None
    )
    if not header_row:
        result["error"] = f"Excel 第 {header_row_idx} 行为空（需要表头行）"
        wb.close()
        return result

    header_map: dict[str, int] = {}
    for idx, val in enumerate(header_row):
        norm = _normalize_column_header(val)
        if norm:
            header_map[norm] = idx

    col_indices: dict[str, int] = {}
    missing_fields: list[str] = []
    for field_name, norm_target in norm_mapping.items():
        if norm_target in header_map:
            col_indices[field_name] = header_map[norm_target]
        else:
            found = False
            for norm_header, h_idx in header_map.items():
                if norm_target in norm_header or norm_header in norm_target:
                    col_indices[field_name] = h_idx
                    found = True
                    break
            if not found:
                missing_fields.append(f"{field_name} (目标列: {column_mapping.get(field_name, '?')})")

    if "article_text" not in col_indices:
        result["error"] = (
            f"无法在 Excel 中找到以下字段对应的列: {', '.join(missing_fields)}。"
            f"可用的表头: {', '.join(header_map.keys())}"
            if missing_fields else "column_mapping 中缺少必需的 'article_text' 字段"
        )
        wb.close()
        return result

    text_col = col_indices["article_text"]
    id_col = col_indices.get("article_id", -1)
    doc_col = col_indices.get("document_name", -1)
    section_col = col_indices.get("section", -1)
    context_col = col_indices.get("additional_context", -1)

    row_count = 0
    document_groups: dict[str, list[dict[str, Any]]] = {}

    data_start = header_row_idx + 1
    for row_idx, row in enumerate(
        ws.iter_rows(min_row=data_start, values_only=True), start=data_start
    ):
        if row_count >= max_rows:
            logger.warning("Reached max_rows limit (%d) for Excel extraction", max_rows)
            break

        if text_col >= len(row) or row[text_col] is None:
            continue
        article_text = str(row[text_col]).strip()
        if not article_text:
            continue

        article_id = ""
        if id_col >= 0 and id_col < len(row) and row[id_col] is not None:
            article_id = str(row[id_col]).strip()

        document_name = ""
        if doc_col >= 0 and doc_col < len(row) and row[doc_col] is not None:
            document_name = str(row[doc_col]).strip()

        section = ""
        if section_col >= 0 and section_col < len(row) and row[section_col] is not None:
            section = str(row[section_col]).strip()

        full_text_parts: list[str] = []
        if context_col >= 0 and context_col < len(row) and row[context_col] is not None:
            ctx = str(row[context_col]).strip()
            if ctx and ctx != article_text:
                full_text_parts.append(ctx)
        full_text_parts.append(article_text)
        full_text = "\n".join(full_text_parts)

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

        group_key = document_name or "_未命名文档_"
        document_groups.setdefault(group_key, []).append(article_dict)

    wb.close()
    result["total_rows"] = row_count
    result["document_groups"] = document_groups
    result["unique_documents"] = sorted(document_groups.keys())

    logger.info(
        "Excel extraction: %d articles across %d documents from %s",
        row_count, len(document_groups), path.name,
    )
    return result


def _col_idx_to_letter(idx: int) -> str:
    """0 基列号 → Excel 列字母(0→A, 25→Z, 26→AA)。"""
    letters = ""
    n = idx
    while n >= 0:
        letters = chr(ord("A") + (n % 26)) + letters
        n = n // 26 - 1
    return letters
