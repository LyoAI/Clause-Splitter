"""Stage 2 — split the regulation into articles (拆条).

Deterministic path first (MinerU → heading inference → per-article chunks,
zero LLM, writes ``split/structure.json`` itself); LLM segmentation as the
fallback for marker-less documents. Produces one generation of seq-ordered
clause rows.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from clause_splitter.compare.episode import ExternalClause
from clause_splitter.compare.loader import clauses_from_document
from clause_splitter.split.structure import analyze_structure
from clause_splitter.utils import get_logger
from .contexts import StageContext, StageError

logger = get_logger()


def _clauses_from_llm(title: str, raw_text: str, llm: Any) -> list[ExternalClause]:
    """LLM segmentation fallback (regex fast-path + flat fallback inside)."""
    structure = analyze_structure(raw_text, llm)
    return [
        ExternalClause(
            document_name=title,
            clause_number=a.article_id,
            section=a.parent_section or "",
            full_text=a.full_text,
        )
        for a in structure.articles
        if a.full_text.strip()
    ]


def _write_structure(reg_dir: Path, title: str, clauses: list[ExternalClause]) -> None:
    """Persist the fallback segmentation in the same format as the MinerU path."""
    out = reg_dir / "split" / "structure.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    articles = [
        {"article_id": c.clause_number, "parent_section": c.section, "full_text": c.full_text}
        for c in clauses
    ]
    out.write_text(
        json.dumps({"document_title": title, "articles": articles}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def run(ctx: StageContext) -> dict[str, Any]:
    src = Path(ctx.regulation["source_path"])
    title = ctx.regulation.get("title") or src.stem

    clauses: list[ExternalClause] = []
    mode = "mineru"
    try:
        clauses = await asyncio.to_thread(clauses_from_document, src, ctx.reg_dir / "split")
    except Exception as e:  # noqa: BLE001 — fallback below
        logger.warning("MinerU split failed for %s, falling back to LLM segmentation: %s", ctx.reg_id, e)

    if not clauses:
        from clause_splitter.config import get_settings
        from clause_splitter.utils.file.doc_parser import extract_text

        mode = "llm"
        raw = await asyncio.to_thread(extract_text, src, max_chars=get_settings().llm_max_input_chars)
        if not raw.strip():
            raise StageError("文档文本抽取为空,无法拆条。")
        # analyze_structure may invoke the (sync) LLM — keep it off the loop.
        clauses = await asyncio.to_thread(_clauses_from_llm, title, raw, ctx.llm)
        if not clauses:
            raise StageError("拆条失败:未识别出任何条款。")
        _write_structure(ctx.reg_dir, title, clauses)

    count = await ctx.repos.clauses.replace_for_regulation(ctx.reg_id, [
        {
            "clause_number": c.clause_number,
            "section": c.section or "",
            "clause_text": c.full_text,
        }
        for c in clauses
    ])
    logger.info("split stage: %d articles (%s) for %s", count, mode, ctx.reg_id)
    return {"article_count": count, "split_mode": mode}
