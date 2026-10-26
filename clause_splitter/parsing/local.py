"""Local plain-text extraction — the fallback when the RAG service cannot parse.

This is *not* a retrieval backend: it extracts text once (pdfplumber /
python-docx / openpyxl / utf-8) so clause splitting can continue. Scanned PDFs
have no local OCR; callers surface them for manual review.
"""

from __future__ import annotations

from pathlib import Path

from clause_splitter.utils import get_logger
from clause_splitter.utils.doc_parser import extract_text

logger = get_logger()

# Unsentinel: extract_text returns placeholder strings instead of raising.
_ERROR_PREFIXES = ("[Parse error", "[Image]", "[Legacy .doc]")


def extract_local_text(path: str | Path) -> str:
    """Full plain text of ``path`` (markdown returned verbatim)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"source file not found: {path}")
    if path.suffix.lower() in (".md", ".markdown"):
        return path.read_text(encoding="utf-8", errors="replace")
    # Large cap = "no truncation"; the parser caches by content hash.
    text = extract_text(path, max_chars=1_000_000_000)
    if not text.strip():
        raise ValueError(f"本地文本抽取为空: {path.name}")
    for prefix in _ERROR_PREFIXES:
        if text.lstrip().startswith(prefix):
            raise ValueError(f"本地无法解析 {path.name}: {text.strip()[:120]}")
    return text
