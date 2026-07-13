"""Extract text from documents (PDF / DOCX / DOC / XLSX / TXT / images).

Parsing results are cached to .cache/docs/ by content hash — re-uploading
the same file skips parsing entirely.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path


# Default cache directory: .cache/docs/ in project root
_CACHE_DIR = Path(__file__).parent.parent.parent / ".cache" / "docs"
_MAX_CACHE_ENTRIES = 100  # auto-clean old entries when exceeded


def extract_text(
    file_path: str | Path,
    max_chars: int = 20000,
    use_cache: bool = True,
) -> str:
    """Auto-detect file type and extract plain text, with optional caching.

    Args:
        file_path: Path to the document.
        max_chars: Maximum characters to extract.
        use_cache: If True, skip parsing when a cached result exists.

    Returns:
        Extracted plain text, or an error placeholder string.
    """
    path = Path(file_path)
    if not path.exists():
        return ""

    # ── Cache lookup by content hash ──────────────────────────────
    if use_cache:
        file_hash = _hash_file(path)
        cached = _read_cache(file_hash)
        if cached is not None:
            return cached[:max_chars]

    # ── Parse ─────────────────────────────────────────────────────
    suffix = path.suffix.lower()

    try:
        if suffix == ".pdf":
            text = _parse_pdf(path, max_chars)
        elif suffix == ".docx":
            text = _parse_docx(path, max_chars)
        elif suffix == ".doc":
            text = _parse_doc(path, max_chars)
        elif suffix in (".xlsx", ".xls"):
            text = _parse_xlsx(path, max_chars)
        elif suffix in (".png", ".jpg", ".jpeg", ".bmp", ".tiff"):
            text = f"[Image] {path.name} — OCR not available, manual review required."
        else:
            text = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception as e:
        text = f"[Parse error: {e}]"

    # ── Write cache ───────────────────────────────────────────────
    if use_cache and text and not text.startswith("[Parse error") and not text.startswith("[Image]"):
        _write_cache(file_hash, text, path.name)

    return text


def clear_cache(older_than_days: float = 0) -> int:
    """Clear the document parse cache.

    Args:
        older_than_days: If > 0, only clear entries older than this many days.
                         If 0, clear all entries.

    Returns:
        Number of entries removed.
    """
    if not _CACHE_DIR.exists():
        return 0

    removed = 0
    cutoff = time.time() - older_than_days * 86400 if older_than_days > 0 else float("inf")

    for cache_file in _CACHE_DIR.glob("*.txt"):
        try:
            if cache_file.stat().st_mtime < cutoff:
                cache_file.unlink()
                removed += 1
        except OSError:
            pass

    return removed


# ── Cache internals ─────────────────────────────────────────────────────

def _hash_file(path: Path) -> str:
    """Compute SHA256 hash of file content (streaming, memory-safe)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def _read_cache(file_hash: str) -> str | None:
    """Read cached text if it exists."""
    cache_path = _CACHE_DIR / f"{file_hash}.txt"
    if not cache_path.exists():
        return None
    try:
        content = cache_path.read_text(encoding="utf-8")
        # First line is metadata comment: # cached: filename @ timestamp
        if content.startswith("# cached:"):
            _, body = content.split("\n", 1)
            return body
        return content
    except Exception:
        return None


def _write_cache(file_hash: str, text: str, original_name: str) -> None:
    """Write parsed text to cache with metadata header."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    header = f"# cached: {original_name} @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    cache_path = _CACHE_DIR / f"{file_hash}.txt"
    cache_path.write_text(header + text, encoding="utf-8")

    # Auto-clean if too many entries
    entries = sorted(_CACHE_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    if len(entries) > _MAX_CACHE_ENTRIES:
        for old in entries[:len(entries) - _MAX_CACHE_ENTRIES]:
            try:
                old.unlink()
            except OSError:
                pass


# ── Parsers ─────────────────────────────────────────────────────────────

def _parse_pdf(path: Path, max_chars: int) -> str:
    """Extract text from PDF files using pdfplumber."""
    import pdfplumber
    texts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                texts.append(t)
            if sum(len(x) for x in texts) >= max_chars:
                break
    return "\n".join(texts)[:max_chars]


def _parse_docx(path: Path, max_chars: int) -> str:
    """Extract text from .docx (Office Open XML) files."""
    from docx import Document
    doc = Document(path)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)[:max_chars]


def _parse_doc(path: Path, max_chars: int) -> str:
    """Extract text from legacy .doc (binary) files.

    Tries multiple backends since old-format .doc is not supported by python-docx.
    """
    import subprocess

    # Strategy 1: antiword (common on Linux/macOS)
    try:
        result = subprocess.run(
            ["antiword", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:max_chars]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Strategy 2: textract (if installed)
    try:
        import textract
        text = textract.process(str(path)).decode("utf-8", errors="replace")
        if text.strip():
            return text.strip()[:max_chars]
    except Exception:
        pass

    # Strategy 3: python-docx — works for misnamed .docx files
    try:
        from docx import Document
        doc = Document(path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        if paragraphs:
            return "\n".join(paragraphs)[:max_chars]
    except Exception:
        pass

    return (
        f"[Legacy .doc] {path.name} — unable to extract text. "
        f"Please convert to .docx or .pdf."
    )


def _parse_xlsx(path: Path, max_chars: int) -> str:
    """Extract text from Excel files (all sheets, tab-separated)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    lines: list[str] = []
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        lines.append(f"[Sheet: {sheet}]")
        for row in ws.iter_rows(values_only=True):
            row_texts = [str(v) for v in row if v is not None]
            if row_texts:
                lines.append("\t".join(row_texts))
    return "\n".join(lines)[:max_chars]
