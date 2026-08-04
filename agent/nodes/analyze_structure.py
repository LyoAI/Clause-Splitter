"""Document structure analysis node — LLM-first with regex fallback.

Strategy (ordered by priority):
1. LLM-based segmentation — handles any document format (第X条, Article X,
   ISO numbering, plain paragraphs, etc.)
2. Regex fast-path — only when LLM is unavailable AND regex covers >80% of text
3. Fallback — treat entire document as one flat article
"""

from __future__ import annotations

import json
import re
from typing import Any

from agent.tools.structure_tools import segment_by_regex, identify_chapters, extract_article_text
from agent.tools.utility_tools import estimate_complexity
from config.settings import get_settings
from models.clause import DocumentStructure, ArticleInfo, ComplexityLabel
from utils import get_logger

logger = get_logger()


async def analyze_structure_node(state: dict) -> dict:
    """Analyze document structure — LLM-first, regex-fallback.

    Args:
        state: AgentState with raw_text.

    Returns:
        Partial state with document_structure, total_articles, progress.
    """
    raw_text: str = state.get("raw_text", "")
    if not raw_text:
        return {
            "status": "failed",
            "error_message": "raw_text is empty",
            "current_step": "Structure analysis failed — empty text",
        }

    logger.info("Analyzing document structure (%d chars)...", len(raw_text))

    # ── Strategy 1: LLM-based segmentation (preferred) ──────────────────
    llm = state.get("llm")
    if llm is not None:
        articles = await _llm_segment(raw_text, llm)
        if articles:
            doc_structure = _build_structure(raw_text, articles)
            logger.info("LLM segmentation: %d articles found", len(articles))
            return _structure_result(doc_structure, raw_text)

    # ── Strategy 2: Regex fast-path ─────────────────────────────────────
    chapters = identify_chapters(raw_text)
    articles = _regex_segment(raw_text)
    if articles:
        # Enrich with full text
        for article in articles:
            try:
                full = extract_article_text(raw_text, article.article_id)
                if full["found"] and full["char_count"] > len(article.full_text):
                    article.full_text = full["full_text"]
            except Exception:
                pass

        # Attach chapter info
        for article in articles:
            seg_start = raw_text.find(article.article_id)
            if seg_start < 0:
                continue
            for ch in chapters.get("chapters", []):
                if ch["start"] <= seg_start < ch["end"]:
                    article.parent_section = ch["title"]
                    break

        doc_structure = _build_structure(raw_text, articles)
        logger.info("Regex segmentation: %d articles", len(articles))
        return _structure_result(doc_structure, raw_text)

    # ── Strategy 3: Flat fallback — treat entire doc as one article ─────
    logger.warning("No article boundaries found — treating as single flat article")
    article = ArticleInfo(
        article_id="全文",
        full_text=raw_text,
        complexity_score=estimate_complexity(raw_text)["complexity_score"],
        complexity_label=ComplexityLabel.COMPLEX,
        estimated_obligation_count=max(1, raw_text.count("。") + raw_text.count(".")),
    )
    doc_structure = DocumentStructure(
        document_title=_extract_title(raw_text),
        articles=[article],
        total_character_count=len(raw_text),
        structure_type="flat",
    )
    return _structure_result(doc_structure, raw_text)


# ── Regex segmentation ─────────────────────────────────────────────────────

def _regex_segment(raw_text: str) -> list[ArticleInfo]:
    """Try regex patterns in priority order; return articles from best match."""
    settings = get_settings()

    best_articles: list[ArticleInfo] = []
    best_coverage = 0.0

    for pattern in settings.article_patterns:
        result = segment_by_regex(raw_text, pattern)
        if not result["success"]:
            continue
        if result["coverage_ratio"] > best_coverage:
            best_coverage = result["coverage_ratio"]
            best_articles = _segments_to_articles(result["segments"])

    threshold = settings.regex_coverage_threshold
    logger.debug("Regex best coverage=%.1f%% (threshold=%.0f%%)", best_coverage * 100, threshold * 100)

    if best_coverage >= threshold and best_articles:
        return best_articles
    return []


def _segments_to_articles(segments: list[dict[str, Any]]) -> list[ArticleInfo]:
    """Convert raw regex segments into ArticleInfo objects."""
    settings = get_settings()
    articles: list[ArticleInfo] = []
    for seg in segments:
        complexity = estimate_complexity(seg.get("text_preview", ""))
        articles.append(ArticleInfo(
            article_id=seg["id"],
            full_text=seg.get("text_preview", ""),
            complexity_score=complexity["complexity_score"],
            complexity_label=(
                ComplexityLabel.COMPLEX
                if complexity["complexity_score"] >= settings.complex_solo_threshold
                else ComplexityLabel.SIMPLE
            ),
            estimated_obligation_count=max(1, complexity["metrics"]["sentence_count"]),
        ))
    return articles


# ── LLM segmentation ───────────────────────────────────────────────────────

async def _llm_segment(raw_text: str, llm: Any) -> list[ArticleInfo] | None:
    """Use LLM to identify article boundaries — works for any document format."""
    from prompts.loader import PromptLoader
    from prompts.split.structure import format_user_prompt

    settings = get_settings()
    loader = PromptLoader()
    system_prompt = loader.get_structure_analysis_prompt()

    # Truncate if too long for a single LLM call
    max_input = settings.llm_max_input_chars
    text_snippet = raw_text[:max_input]
    user_prompt = format_user_prompt(text_snippet, len(raw_text), max_input)

    try:
        response = llm.invoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        content = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        logger.warning("LLM structure analysis failed: %s", e)
        return None

    # Parse JSON from LLM output
    parsed = _extract_json(content)
    if not parsed:
        logger.warning("LLM structure analysis returned unparseable output")
        return None

    articles_data = parsed.get("articles", [])
    if not articles_data:
        return None

    articles: list[ArticleInfo] = []
    for item in articles_data:
        try:
            article_id = str(item.get("article_id", ""))
            full_text = str(item.get("full_text", ""))

            # If LLM gave us a snippet, try to extract the full text
            if len(full_text) < 20:
                extraction = extract_article_text(raw_text, article_id)
                if extraction["found"]:
                    full_text = extraction["full_text"]

            complexity = item.get("complexity_score", 1.0)
            articles.append(ArticleInfo(
                article_id=article_id,
                full_text=full_text,
                parent_section=item.get("parent_section"),
                complexity_score=float(complexity),
                complexity_label=(
                    ComplexityLabel.COMPLEX if float(complexity) >= settings.complex_solo_threshold
                    else ComplexityLabel.SIMPLE
                ),
                estimated_obligation_count=max(1, int(item.get("estimated_obligation_count", 1))),
            ))
        except Exception as e:
            logger.warning("Skipping malformed article from LLM: %s", e)

    return articles or None


# ── Helpers ─────────────────────────────────────────────────────────────────

def _build_structure(raw_text: str, articles: list[ArticleInfo]) -> DocumentStructure:
    """Assemble a DocumentStructure from articles."""
    settings = get_settings()

    # Use first article pattern from settings to detect "standard" structure
    first_pattern = settings.article_patterns[0] if settings.article_patterns else r"第[一-鿿\d]+条"
    standard_pattern = re.compile(first_pattern)

    # Detect structure type
    if any(a.parent_section for a in articles):
        structure_type = "chapter_based"
    elif all(standard_pattern.match(a.article_id) for a in articles):
        structure_type = "standard"
    else:
        structure_type = "mixed"

    return DocumentStructure(
        document_title=_extract_title(raw_text, standard_pattern),
        articles=articles,
        total_character_count=len(raw_text),
        structure_type=structure_type,
    )


def _extract_title(text: str, article_pattern: re.Pattern | None = None) -> str:
    """Heuristic: first short non-article line is probably the title.

    Uses the configured article pattern to skip article-like lines.
    """
    if article_pattern is None:
        settings = get_settings()
        first_pat = settings.article_patterns[0] if settings.article_patterns else r"第[一-鿿\d]+条"
        article_pattern = re.compile(first_pat)

    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if len(line) < 100 and not article_pattern.match(line):
            return line
    return ""


def _extract_json(content: str) -> dict | None:
    """Robust JSON extraction from LLM output (code blocks, raw, etc.)."""
    # Try code block first
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", content)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try raw
    try:
        return json.loads(content.strip())
    except json.JSONDecodeError:
        pass
    # Try first {...} block
    m = re.search(r"\{[\s\S]+\}", content)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


def _structure_result(doc: DocumentStructure, raw_text: str) -> dict:
    """Build the return dict for the graph state."""
    milestones = get_settings().progress_milestones
    logger.info("Structure analysis complete: %d articles, type=%s",
                 len(doc.articles), doc.structure_type)
    return {
        "document_structure": doc.model_dump(),
        "total_articles": len(doc.articles),
        "progress": milestones["structure_complete"],
        "current_step": f"Structure done — {len(doc.articles)} articles found",
        "status": "analyzing_structure",
    }
