"""jieba word-level tokenizer (Chinese) with ASCII case-folding (English).

Anserini's default analyzer tokenizes Chinese as single characters (unigram);
``-language zh`` uses Lucene's CJKAnalyzer (overlapping bigrams). Neither does
word segmentation. Word-level tokens (``数据安全`` / ``管理制度`` / ``个人信息``)
match far better.

English handling: jieba keeps ASCII runs as single tokens, so English text is
tokenized on word boundaries too — but WhitespaceAnalyzer does no case-folding
or stemming, so tokens are lowercased here to make matching case-insensitive
(``Data Security`` vs ``data security``). Trade-off: no English stemming
(``policies`` ≠ ``policy``); if that ever matters, swap this module for a
stemming tokenizer and bump ``ANALYZER_VERSION``.

Strategy:
    We pre-tokenize with jieba and join the words with spaces, then index with
    Lucene's ``-pretokenized`` mode (WhitespaceTokenizer) so each word survives
    as a single term. The searcher applies the *same* tokenization to queries
    and uses a WhitespaceAnalyzer so query terms align with the index.

    ⚠️  You cannot use the default/CJK analyzer on a ``-pretokenized`` index —
    the query and index tokenization must match exactly or retrieval silently
    returns zero hits.

``ANALYZER_VERSION`` is recorded in the BM25 index dir. If it ever changes,
the searcher forces a full rebuild — mixing tokenizers inside one Lucene
index would silently corrupt retrieval.
"""

from __future__ import annotations

import logging

from chillrag.utils import get_logger

logger = get_logger()

# Bump when the tokenization pipeline changes → forces a full rebuild of
# existing indexes (checked against the index's recorded version).
# v2: ASCII tokens lowercased (English case-insensitive matching). Bumping
# this forces a full rebuild of every existing index — old indexes were built
# with case-sensitive terms and must not be mixed with v2 queries.
ANALYZER_VERSION = "jieba-pretokenized-v2"

_jieba = None


def _require_jieba():
    """Lazily import jieba (pure-Python, no compiled dependencies).

    Deferred so that merely importing this module doesn't pull in jieba —
    commands that never touch the index stay fast.
    """
    global _jieba
    if _jieba is None:
        try:
            import jieba
        except ImportError as e:  # noqa: BLE001
            raise RuntimeError(
                "中文分词需要 jieba 包。请执行: uv pip install jieba\n"
                f"原始错误: {e}"
            ) from e
        jieba.setLogLevel(logging.WARNING)  # silence "Building prefix dict" noise
        _jieba = jieba
    return _jieba


def tokenize(text: str) -> list[str]:
    """jieba-cut a string into words (whitespace / empty tokens removed).

    ASCII tokens are lowercased so English matches case-insensitively under
    the WhitespaceAnalyzer (which does no case-folding itself). jieba keeps
    ASCII runs as single tokens, so this is safe for mixed zh/en text.

    Args:
        text: Raw text (may be empty; Chinese and mixed zh/en both fine).

    Returns:
        List of word tokens, e.g.
        ``["应当", "建立", "数据安全", "管理制度"]`` or ``["data", "security"]``.
    """
    if not text or not text.strip():
        return []
    jieba = _require_jieba()
    return [
        w.lower() if w.isascii() else w
        for w in jieba.cut(text)
        if w.strip()
    ]


def to_pretokenized(text: str) -> str:
    """jieba-cut then space-join — the format Lucene ``-pretokenized`` expects.

    e.g. ``"建立健全数据安全管理制度"`` → ``"建立 健全 数据安全 管理制度"``
    """
    return " ".join(tokenize(text))
