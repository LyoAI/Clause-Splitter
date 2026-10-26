"""文档结构分析 —— LLM 优先、正则回退、flat 兜底.

同时提供拆分侧共用的确定性辅助:正则分段 / 章节识别 / 复杂度估计。
"""

from __future__ import annotations

import json
import re
from typing import Any

from clause_splitter.config import get_settings
from clause_splitter.models import ArticleInfo, ComplexityLabel, DocumentStructure
from clause_splitter.prompts import load
from clause_splitter.utils import get_logger

logger = get_logger()


def analyze_structure(raw_text: str, llm: Any = None) -> DocumentStructure:
    """分析文档结构 —— LLM 优先, 正则回退, flat 兜底。"""
    if not raw_text or not raw_text.strip():
        raise ValueError("raw_text 为空，无法分析文档结构")

    logger.info("Analyzing document structure (%d chars)...", len(raw_text))

    # 1) LLM segmentation
    if llm is not None:
        articles = _llm_segment(raw_text, llm)
        if articles:
            doc_structure = _build_structure(raw_text, articles)
            logger.info("LLM segmentation: %d articles found", len(articles))
            return doc_structure

    # 2) regex fast path
    chapters = identify_chapters(raw_text)
    articles = _regex_segment(raw_text)
    if articles:
        # 分段时已带完整原文,仅需补充章节归属
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
        return doc_structure

    # 3) flat fallback
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
    return doc_structure


def _llm_segment(raw_text: str, llm: Any) -> list[ArticleInfo] | None:
    """用 LLM 识别条款边界 —— 兼容任意文档格式。"""
    settings = get_settings()
    max_input = settings.llm_max_input_chars
    text_snippet = raw_text[:max_input]
    user_prompt = f"请分析以下文档的结构：\n\n{text_snippet}"
    if len(raw_text) > max_input:
        user_prompt += (
            f"\n\n（注意：原文共 {len(raw_text)} 字符，此处仅展示前 {max_input} 字符。"
            f"请基于已有内容识别所有可见条款，并用'...'标记截断位置。）"
        )

    try:
        response = llm.invoke([
            {"role": "system", "content": load("split_structure")},
            {"role": "user", "content": user_prompt},
        ])
        content = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        logger.warning("LLM structure analysis failed: %s", e)
        return None

    parsed = _extract_json(content)
    if not parsed:
        logger.warning("LLM structure analysis returned unparseable output")
        return None

    articles_data = parsed.get("articles", [])
    if not articles_data:
        return None

    settings = get_settings()
    articles: list[ArticleInfo] = []
    for item in articles_data:
        try:
            article_id = str(item.get("article_id", ""))
            full_text = str(item.get("full_text", ""))

            # LLM 只给了片段 → 尝试从原文抽取完整文本
            if len(full_text) < 20:
                extraction = extract_article_text(raw_text, article_id)
                if extraction["found"]:
                    full_text = extraction["full_text"]

            complexity = float(item.get("complexity_score", 1.0))
            articles.append(ArticleInfo(
                article_id=article_id,
                full_text=full_text,
                parent_section=item.get("parent_section"),
                complexity_score=complexity,
                complexity_label=(
                    ComplexityLabel.COMPLEX if complexity >= settings.complex_solo_threshold
                    else ComplexityLabel.SIMPLE
                ),
                estimated_obligation_count=max(1, int(item.get("estimated_obligation_count", 1))),
            ))
        except Exception as e:
            logger.warning("Skipping malformed article from LLM: %s", e)

    return articles or None


def _regex_segment(raw_text: str) -> list[ArticleInfo]:
    """按优先级尝试各正则;返回覆盖率最高且 >= 阈值的结果。"""
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
    settings = get_settings()
    articles: list[ArticleInfo] = []
    for seg in segments:
        full_text = seg.get("text") or seg.get("text_preview", "")
        complexity = estimate_complexity(full_text)
        articles.append(ArticleInfo(
            article_id=seg["id"],
            full_text=full_text,
            complexity_score=complexity["complexity_score"],
            complexity_label=(
                ComplexityLabel.COMPLEX
                if complexity["complexity_score"] >= settings.complex_solo_threshold
                else ComplexityLabel.SIMPLE
            ),
            estimated_obligation_count=max(1, complexity["metrics"]["sentence_count"]),
        ))
    return articles


def _build_structure(raw_text: str, articles: list[ArticleInfo]) -> DocumentStructure:
    settings = get_settings()
    first_pattern = settings.article_patterns[0] if settings.article_patterns else r"第[一-鿿\d]+条"
    standard_pattern = re.compile(first_pattern)

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
    """启发式:第一个短的非条款行大概率是标题。"""
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
    """从 LLM 输出健壮提取 JSON(代码块 / 裸 JSON / 首个 {} 块)。"""
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


def segment_by_regex(text: str, pattern: str) -> dict[str, Any]:
    """用给定正则对文本分段,返回分段列表与覆盖率。"""
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return {
            "success": False, "total_segments": 0, "segments": [],
            "coverage_ratio": 0.0, "message": f"无效的正则表达式: {e}",
        }

    matches = list(compiled.finditer(text))
    if not matches:
        return {
            "success": False, "total_segments": 0, "segments": [],
            "coverage_ratio": 0.0,
            "message": f"模式 '{pattern}' 未匹配到任何内容",
        }

    segments: list[dict[str, Any]] = []
    for i, match in enumerate(matches):
        # 取完整匹配作为条款号("第X条"/"（X）"),与 LLM 路径输出一致
        article_id = match.group(0)
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg_text = text[start:end].strip()
        segments.append({
            "id": article_id,
            "start": start,
            "end": end,
            "char_count": end - start,
            "text": seg_text,
            "text_preview": seg_text[:150] + ("..." if len(seg_text) > 150 else ""),
        })

    first_start = matches[0].start()
    last_end = matches[-1].start()
    total_len = len(text)
    coverage = (last_end - first_start) / max(total_len, 1)

    return {
        "success": True,
        "total_segments": len(segments),
        "segments": segments,
        "coverage_ratio": round(coverage, 3),
        "message": f"匹配到 {len(segments)} 个段落，覆盖率 {coverage:.1%}",
    }


def identify_chapters(text: str) -> dict[str, Any]:
    """识别文档的章/节结构。"""
    settings = get_settings()

    for pattern, name in settings.chapter_patterns:
        matches = list(re.finditer(pattern, text))
        if len(matches) >= 1:
            chapters = []
            for i, match in enumerate(matches):
                title = match.group(0).strip()
                start = match.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                chapters.append({"title": title, "start": start, "end": end})
            return {"chapters": chapters, "pattern_used": name, "total_chapters": len(chapters)}

    return {"chapters": [], "pattern_used": "none", "total_chapters": 0}


def extract_article_text(text: str, article_id: str, next_pattern: str | None = None) -> dict[str, Any]:
    """抽取指定条款的完整文本(到下一条款边界为止)。"""
    settings = get_settings()

    escaped_id = re.escape(article_id)
    match = re.compile(rf"{escaped_id}\s*[^\n]*").search(text)
    if not match:
        return {"found": False, "article_id": article_id, "full_text": "", "char_count": 0}

    start = match.start()
    search_from = start + len(match.group())

    if next_pattern is not None:
        patterns = [next_pattern]
    else:
        # Match the same boundary pattern the id came from, so sub-items inside
        # the article body are not mistaken for the next article.
        patterns = [p for p in settings.article_patterns if re.compile(p).search(article_id)]
        if not patterns:
            patterns = settings.article_patterns[:1] or [r"第[一-鿿\d]+条"]

    end = len(text)
    for p in patterns:
        m = re.compile(p).search(text, search_from)
        if m and m.start() < end:
            end = m.start()

    full_text = text[start:end].strip()
    return {"found": True, "article_id": article_id, "full_text": full_text, "char_count": len(full_text)}


def estimate_complexity(article_text: str) -> dict[str, Any]:
    """结构启发式复杂度(0-10);LLM 不可用时的轻量回退。"""
    settings = get_settings()
    char_count = len(article_text)

    comma_count = article_text.count("，") + article_text.count("；") + article_text.count(",") + article_text.count(";")

    sub_items = re.findall(r"[（(][一二三四五六七八九十\d]+[）)]", article_text)
    sub_item_count = len(sub_items)

    sentence_count = (
        article_text.count("。") + article_text.count(".") +
        article_text.count("；") + article_text.count(";") +
        article_text.count("\n")
    )

    score = 0.0

    for threshold, points in settings.complexity_length_tiers:
        if char_count > threshold:
            score += points
            break

    density = comma_count / max(char_count, 1) * 100
    if density > 8:
        score += 3.0
    elif density > 4:
        score += 2.0
    elif density > 1:
        score += 1.0

    for threshold, points in settings.complexity_sub_item_tiers:
        if sub_item_count > threshold:
            score += points
            break

    avg_sentence_len = char_count / max(sentence_count, 1)
    if avg_sentence_len > 150:
        score += 2.0
    elif avg_sentence_len > 80:
        score += 1.0

    score = min(score, 10.0)

    return {
        "complexity_score": round(score, 1),
        "complexity_label": "complex" if score >= settings.complex_solo_threshold else "simple",
        "metrics": {
            "char_count": char_count,
            "comma_count": comma_count,
            "sub_item_count": sub_item_count,
            "sentence_count": sentence_count,
        },
    }


def count_tokens(text: str, model: str = "default") -> dict[str, Any]:
    """启发式 token 估算(中文约 1.5 字/token,英文约 4 字/token)。"""
    char_count = len(text)
    chinese_chars = len(re.findall(r"[一-鿿]", text))
    non_chinese = char_count - chinese_chars
    estimated = int(chinese_chars / 1.5 + non_chinese / 4.0)

    return {
        "estimated_tokens": max(estimated, 1),
        "char_count": char_count,
        "method": "heuristic (~1.5 chars/token CN, ~4 chars/token EN)",
    }
