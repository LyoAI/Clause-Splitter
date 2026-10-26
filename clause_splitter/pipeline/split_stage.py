"""Stage 2 — split the regulation into articles (拆条).

Four-layer ladder, most flexible first:
① LLM infers the document's numbering scheme (one structured call);
② code validates the scheme and applies it (generalized headings pass);
③ builtin 编章节条/顿号 scheme (``compare.headings``, deterministic);
④ ``analyze_structure`` text segmentation (LLM/regex) for marker-less docs.
Each layer's output must pass ``scheme.scheme_output_sane`` before it wins.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from clause_splitter.compare.episode import ExternalClause
from clause_splitter.compare.headings import infer_regulation_headings
from clause_splitter.compare.loader import clauses_from_blocks
from clause_splitter.parsing.router import parse_document
from clause_splitter.rag import RagService
from clause_splitter.split import scheme
from clause_splitter.split.structure import analyze_structure
from clause_splitter.utils import get_logger
from . import doc_profile
from .contexts import StageContext, StageError

logger = get_logger()


async def clauses_from_file(
    src: Path, llm: Any, service: RagService | None = None
) -> tuple[list[ExternalClause], str, list[dict], str] | None:
    """Layers ①–③ over parsed blocks → (clauses, layer, scheme_json, source)."""
    parsed = await asyncio.to_thread(parse_document, src, src.stem, service=service)
    blocks = parsed.blocks
    doc_id = src.stem

    rules = await scheme.infer_scheme_llm(blocks, llm)
    if rules:
        try:
            clauses = await asyncio.to_thread(
                clauses_from_blocks, doc_id, scheme.apply_scheme(blocks, rules)
            )
        except Exception as exc:  # noqa: BLE001 — bad regexes etc, fall through
            logger.warning("llm scheme apply failed: %s", exc)
            clauses = []
        if scheme.scheme_output_sane(clauses):
            return clauses, "llm_scheme", [r.to_json() for r in rules], parsed.source
        logger.warning("llm scheme output failed sanity, trying builtin scheme")

    try:
        clauses = await asyncio.to_thread(
            clauses_from_blocks, doc_id, infer_regulation_headings(blocks)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("builtin scheme failed: %s", exc)
        return None
    if scheme.scheme_output_sane(clauses):
        return clauses, "builtin", [], parsed.source
    return None


def _clauses_from_llm(title: str, raw_text: str, llm: Any) -> list[ExternalClause]:
    """Layer ④ fallback (regex fast-path + flat fallback inside)."""
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


def _write_structure(
    reg_dir: Path, title: str, clauses: list[ExternalClause],
    layer: str = "llm_segment", scheme_json: list[dict] | None = None,
    parse_source: str = "local",
) -> None:
    """Persist the segmentation + which ladder layer / parser produced it."""
    out = reg_dir / "split" / "structure.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    articles = [
        {"article_id": c.clause_number, "parent_section": c.section, "full_text": c.full_text}
        for c in clauses
    ]
    out.write_text(
        json.dumps(
            {"document_title": title, "articles": articles,
             "split_layer": layer, "scheme": scheme_json or [],
             "parse_source": parse_source},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


async def run(ctx: StageContext) -> dict[str, Any]:
    src = Path(ctx.regulation["source_path"])
    title = ctx.regulation.get("title") or src.stem

    clauses: list[ExternalClause] = []
    layer, scheme_json, parse_source = "llm_segment", [], "local"
    try:
        ladder = await clauses_from_file(src, ctx.llm, ctx.rag)
        if ladder:
            clauses, layer, scheme_json, parse_source = ladder
    except Exception as e:  # noqa: BLE001 — any block-path failure falls to ④
        logger.warning("block split failed for %s, falling back to text segmentation: %s", ctx.reg_id, e)

    if not clauses:
        from clause_splitter.config import get_settings
        from clause_splitter.utils.doc_parser import extract_text

        raw = await asyncio.to_thread(extract_text, src, max_chars=get_settings().llm_max_input_chars)
        if not raw.strip():
            raise StageError("文档文本抽取为空,无法拆条。")
        # analyze_structure may invoke the (sync) LLM — keep it off the loop.
        clauses = await asyncio.to_thread(_clauses_from_llm, title, raw, ctx.llm)
        if not clauses:
            raise StageError("拆条失败:未识别出任何条款。")

    _write_structure(
        ctx.reg_dir, title, clauses, layer=layer, scheme_json=scheme_json,
        parse_source=parse_source,
    )
    keywords = await asyncio.to_thread(
        lambda: [doc_profile.term_keywords(c.full_text) for c in clauses]
    )
    count = await ctx.repos.clauses.replace_for_regulation(ctx.reg_id, [
        {
            "clause_number": c.clause_number,
            "section": c.section or "",
            "clause_text": c.full_text,
            "term_keyword": keywords[i],
        }
        for i, c in enumerate(clauses)
    ])
    logger.info("split stage: %d articles (%s) for %s", count, layer, ctx.reg_id)
    ctx.progress(0.8, "生成文档摘要")
    await doc_profile.ensure(ctx, texts=[c.full_text for c in clauses])
    return {"article_count": count, "split_mode": layer}
