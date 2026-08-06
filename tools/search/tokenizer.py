"""jieba word-level tokenizer for Chinese BM25 search.

Anserini's default analyzer tokenizes Chinese as single characters (unigram);
``-language zh`` uses Lucene's CJKAnalyzer (overlapping bigrams). Neither does
word segmentation. For legal/regulatory text, word-level tokens
(``数据安全`` / ``管理制度`` / ``个人信息``) match far better.

Strategy:
    We pre-tokenize with jieba and join the words with spaces, then index with
    Lucene's ``-pretokenized`` mode (WhitespaceTokenizer) so each word survives
    as a single term. The searcher applies the *same* tokenization to queries
    and uses a WhitespaceAnalyzer so query terms align with the index.

    ⚠️  You cannot use the default/CJK analyzer on a ``-pretokenized`` index —
    the query and index tokenization must match exactly or retrieval silently
    returns zero hits.

``ANALYZER_VERSION`` is recorded in the index manifest. If it ever changes,
smart-append must force a full rebuild — mixing tokenizers inside one Lucene
index would silently corrupt retrieval.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Bump when the tokenization pipeline changes → forces a full rebuild of
# existing indexes (checked against the manifest by the index manager).
ANALYZER_VERSION = "jieba-pretokenized-v1"

_jieba = None


def _require_jieba():
    """Lazily import jieba (pure-Python, no compiled dependencies).

    Deferred so that merely importing this module doesn't pull in jieba —
    commands that never touch the index (e.g. ``run.py --list``) stay fast.
    """
    global _jieba
    if _jieba is None:
        try:
            import jieba
        except ImportError as e:  # noqa: BLE001
            raise RuntimeError(
                "中文分词需要 jieba 包。请执行: uv pip install jieba\n"
                "内网离线机器可从外网拷贝 jieba 的纯 Python wheel，用 U 盘传输后 "
                "pip install 即可（jieba 无编译依赖）。\n"
                f"原始错误: {e}"
            ) from e
        jieba.setLogLevel(logging.WARNING)  # silence "Building prefix dict" noise
        _jieba = jieba
    return _jieba


def tokenize(text: str) -> list[str]:
    """jieba-cut a string into words (whitespace / empty tokens removed).

    Args:
        text: Raw Chinese text (may be empty).

    Returns:
        List of word tokens, e.g. ``["应当", "数据安全", "管理"]``.
    """
    if not text or not text.strip():
        return []
    jieba = _require_jieba()
    return [w for w in jieba.cut(text) if w.strip()]


def to_pretokenized(text: str) -> str:
    """jieba-cut then space-join — the format Lucene ``-pretokenized`` expects.

    e.g. ``"建立健全数据安全管理制度"`` → ``"应当 建立健全 数据安全 管理制度"``
    """
    return " ".join(tokenize(text))
