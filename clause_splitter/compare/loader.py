"""外规条款装载 —— 目录 / JSON / 文本文档三种来源 → ExternalClause 列表.

供 ``run_compare.py`` 与 FastAPI 薄壳共用,避免根脚本依赖。
"""

from __future__ import annotations

import json
from pathlib import Path

from clause_splitter.utils import get_logger
from ._sources import (
    clause_from_split_result_file,
    clause_from_structure_file,
    scan_clause_source_files,
)
from .episode import ExternalClause

logger = get_logger()


def _clean_doc_name(name: str) -> str:
    """去掉制度名里的文件后缀(旧拆分结果把文件名存成 document_name)。"""
    for suffix in (".docx", ".doc", ".pdf", ".txt", ".xlsx", ".xls"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def _obligations_by_clause(path: Path) -> dict[str, list[dict]]:
    """从拆分结果 JSON 按 clause_number 归并义务 dict(供 episode 参考)。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        obls = data
    elif isinstance(data, dict):
        obls = data.get("obligations") or []
    else:
        return {}
    grouped: dict[str, list[dict]] = {}
    for obl in obls:
        if not isinstance(obl, dict):
            continue
        clause = str(obl.get("clause_number") or "").strip() or "全文"
        grouped.setdefault(clause, []).append(obl)
    return grouped


def external_clauses_from_file(path: Path) -> list[ExternalClause]:
    """从一份 structure.json / 拆分结果 JSON 装载外规条款。"""
    name = path.name
    obligations_by_clause: dict[str, list[dict]] = {}
    if name.endswith("_structure.json"):
        clause_texts = clause_from_structure_file(path)
    else:
        clause_texts = clause_from_split_result_file(path)
        obligations_by_clause = _obligations_by_clause(path)

    clauses: list[ExternalClause] = []
    for c in clause_texts:
        clause_number = str(c["clause_number"] or "").strip() or "全文"
        clauses.append(ExternalClause(
            document_name=_clean_doc_name(str(c["doc"] or "").strip()),
            clause_number=clause_number,
            section=str(c.get("section") or "").strip(),
            full_text=str(c.get("full_text") or "").strip(),
            obligations=obligations_by_clause.get(clause_number, []),
        ))
    return clauses


def _external_clause_from_chunk(doc_id: str, chunk) -> ExternalClause:
    """内规同款按条分块 → 外规条款:每条 chunk = 一条 ExternalClause。

    chunk 来自 ``chunk_doc``(MinerU + 层级标题检测,确定性,零 LLM 解构)。
    条款号取 section 路径末级标题(如 "第一章 总则 > 第一条" → "第一条")。
    """
    section = str(chunk.meta.get("section", "") or "").strip()
    leaf = section.split(" > ")[-1].strip() if " > " in section else section
    return ExternalClause(
        document_name=doc_id,
        clause_number=leaf or "全文",
        section=section or None,
        full_text=chunk.text,
        obligations=[],
    )


def _write_external_structure(out_dir: Path, doc_id: str, clauses: list[ExternalClause]) -> Path:
    """把按条分块结果落盘为 ``{stem}_structure.json``(与拆分产物同格式,可回看/复用)。

    之后把 ``--external`` 指向这个文件时,会走 JSON 直读分支,不再重新 MinerU。
    """
    articles = [
        {
            "article_id": c.clause_number,
            "parent_section": c.section or "",
            "full_text": c.full_text,
        }
        for c in clauses
    ]
    path = out_dir / f"{doc_id}_structure.json"
    path.write_text(
        json.dumps({"document_title": doc_id, "articles": articles}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("外规条款落盘: %s (%d 条)", path, len(clauses))
    return path


async def load_external_clauses(external: str, output_dir: str, llm) -> list[ExternalClause]:
    """装载外规条款列表(目录 / JSON / 文本文档三种来源)。

    Args:
        external: 目录(扫描全部条款来源) / ``*_structure.json`` /
            拆分结果 JSON / docx/pdf/txt(自动 MinerU 解析 + 按条分块)。
        output_dir: 自动解析时的产物目录(写 ``{stem}_structure.json``)。
        llm: 保留参数(JSON/目录/分块路径均不调用 LLM,仅保持签名兼容)。

    注意:async 函数。调用方必须在 async 上下文里 ``await``(两个调用方
    ``run_compare._run`` 与 FastAPI ``_run_compare`` 均为 async)。
    docx/pdf/txt 走与内规库同一套解析链(MinerU → 标题检测 → 按条 chunk,
    确定性,零 LLM 解构);MinerU 原始产物落 ``.cache/external/md/``。
    """
    path = Path(external)
    if path.is_dir():
        clauses: list[ExternalClause] = []
        for f in scan_clause_source_files(path):
            clauses.extend(external_clauses_from_file(f))
        return clauses

    if path.suffix.lower() == ".json":
        clauses = external_clauses_from_file(path)
        if clauses:
            return clauses
        raise SystemExit(f"无法从 JSON 读取外规条款: {path}")

    # docx/pdf/txt → MinerU 解析 + 按条分块(与内规库同一套解析链,零 LLM 解构)。
    # 解析质量高于轻量 extract_text,且按"第X条"标题确定性分 chunk,不做义务解构。
    from clause_splitter.config import MINERU_MD_DIRNAME, doc_cache_dir
    from clause_splitter.tools.parser.chunker import chunk_doc
    from clause_splitter.tools.parser.headings import infer_regulation_headings
    from clause_splitter.tools.parser.mineru_parse import parse_docs

    out_dir = Path(output_dir) if output_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"═══ 外规 MinerU 解析: {path.name} ═══")
    # MinerU 原始 md / content_list 落盘于 .cache/external/md/<stem>/。
    md_dir = doc_cache_dir("external") / MINERU_MD_DIRNAME
    blocks_map = parse_docs([path], out_dir=md_dir)
    blocks = infer_regulation_headings(blocks_map[path.name])

    doc_id = _clean_doc_name(path.name)
    clauses = [
        c for c in (_external_clause_from_chunk(doc_id, chunk) for chunk in chunk_doc(doc_id, blocks))
        if c.full_text.strip()
    ]
    if not clauses:
        raise SystemExit(f"外规 {path.name} MinerU 解析后未产生任何条款分块,无法比对。")
    print(f"MinerU 解析 + 按条分块完成: {len(clauses)} 条\n")

    # 落盘一份与拆分产物同格式的 structure.json,供回看 / 复用(--external 指它则跳过 MinerU)。
    _write_external_structure(out_dir, doc_id, clauses)
    return clauses
