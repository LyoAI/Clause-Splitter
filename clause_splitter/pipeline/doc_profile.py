"""Document-level profile — one LLM call for ``docAbstract`` / ``docKeyword``.

``termKeyword`` stays local. Failures degrade to deterministic fallbacks and
never fail the chain.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path
from typing import Any

from config.config_app import CONFIG_APP

from clause_splitter.config import DOC_PROFILE_TIMEOUT_S
from clause_splitter.prompts import load
from clause_splitter.utils import get_logger, norm_str
from clause_splitter.utils.doc_parser import extract_text
from ..compare.ground import parse_answer_json
from .contexts import StageContext

logger = get_logger()

#: Upper bound on the text sent to the summary call (chars).
PROFILE_MAX_CHARS = 30000


def build_messages(reg: dict[str, Any], text: str) -> list[dict[str, str]]:
    title = reg.get("title") or ""
    user = f"【法规名称】{title or '(未命名)'}\n【法规全文】\n{text}"
    return [
        {"role": "system", "content": load("doc_profile")},
        {"role": "user", "content": user},
    ]


def parse_profile(content: str) -> dict[str, str] | None:
    """Parse the ``<answer>`` JSON; None when the abstract is unusable."""
    parsed = parse_answer_json(content)
    if not isinstance(parsed, dict):
        return None
    abstract = norm_str(parsed.get("abstract"))
    if not abstract:
        return None
    raw = parsed.get("keywords")
    if isinstance(raw, str):
        raw = raw.replace("，", ",").split(",")
    if not isinstance(raw, list):
        raw = []
    keywords: list[str] = []
    for item in raw:
        word = norm_str(item)
        if len(word) >= 2 and word not in keywords:
            keywords.append(word)
    return {"abstract": abstract, "keyword": ", ".join(keywords[:10])}


async def ensure(ctx: StageContext, texts: list[str] | None = None) -> dict[str, str]:
    """生成并持久化 ``doc_abstract`` / ``doc_keyword``;失败时返回 ``{}``。"""
    try:
        reg = await ctx.repos.regulations.get(ctx.reg_id) or ctx.regulation
        if reg.get("doc_abstract") and reg.get("doc_keyword"):
            return {
                "doc_abstract": reg["doc_abstract"],
                "doc_keyword": reg["doc_keyword"],
            }
        if ctx.llm is None:
            return {}

        text = _sample_text(texts)
        if not text:
            src = reg.get("source_path")
            if src:
                text = await asyncio.to_thread(
                    extract_text, Path(src), max_chars=CONFIG_APP.applicability_max_chars,
                )
        text = (text or "").strip()[:PROFILE_MAX_CHARS]
        if not text:
            return {}

        resp = await asyncio.wait_for(
            ctx.llm.ainvoke(build_messages(reg, text)),
            timeout=DOC_PROFILE_TIMEOUT_S,
        )
        content = resp.content if hasattr(resp, "content") else str(resp)
        profile = parse_profile(content)
        if profile is None:
            logger.warning("doc profile unusable for %s, falling back", ctx.reg_id)
            return {}

        fields = {
            "doc_abstract": profile["abstract"],
            "doc_keyword": profile["keyword"],
            "doc_profile_state": "llm",
        }
        await ctx.repos.regulations.update(ctx.reg_id, fields)
        ctx.regulation.update(fields)
        return fields
    except Exception as exc:  # noqa: BLE001 — profile is best-effort
        logger.warning("doc profile failed for %s: %s", ctx.reg_id, exc)
        return {}


def _sample_text(texts: list[str] | None) -> str:
    if not texts:
        return ""
    return "\n".join(t for t in texts if t).strip()


# ── local fallbacks (no LLM) ───────────────────────────────────────────
@lru_cache(maxsize=1024)
def term_keywords(text: str, top_k: int = 5) -> str:
    """Clause keywords via jieba; empty string when nothing usable."""
    return _extract_keywords(text, top_k)


@lru_cache(maxsize=256)
def doc_keywords(text: str, top_k: int = 8) -> str:
    """Document keywords via the same local extractor."""
    return _extract_keywords(text, top_k)


def _extract_keywords(text: str, top_k: int) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    words = _jieba_keywords(body, top_k)
    if not words:
        from clause_splitter.compare.retrieve import keywords

        words = [w for w in keywords(body) if len(w) >= 2][:top_k]
    return ", ".join(words)


def _jieba_keywords(text: str, top_k: int) -> list[str]:
    try:
        import jieba.analyse

        tags = jieba.analyse.extract_tags(text, topK=top_k * 3)
    except Exception:  # noqa: BLE001 — missing jieba or tokenizer error
        return []
    out: list[str] = []
    for tag in tags:
        word = str(tag).strip()
        if len(word) >= 2 and word not in out:
            out.append(word)
        if len(out) >= top_k:
            break
    return out


def fallback_abstract(text: str, limit: int = 200) -> str:
    """Deterministic abstract when the summary call was skipped or failed."""
    body = " ".join((text or "").split())
    if not body:
        return ""
    return body if len(body) <= limit else body[:limit].rstrip() + "…"
