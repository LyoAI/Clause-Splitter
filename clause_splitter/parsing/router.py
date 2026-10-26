"""External document parsing: remote first, local plain-text fallback.

The remote RAG service owns high-fidelity parsing (layout, OCR, tables). When
it is not configured, times out, or returns nothing usable, the fallback
extracts plain text locally so splitting can still run. The chosen path is
recorded as ``parse_source`` in ``structure.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clause_splitter.config import PARSE_FALLBACK
from clause_splitter.rag import RagError, RagService, get_rag_service
from clause_splitter.utils import get_logger

from .blocks import Block, blocks_from_markdown, blocks_from_remote, blocks_from_text
from .local import extract_local_text
from .normalize import normalize_text

logger = get_logger()


@dataclass
class ParsedDocument:
    """Blocks plus provenance for one external document."""

    doc_id: str
    blocks: list[Block]
    text: str
    source: str  # "remote" | "local"


def parse_document(
    path: str | Path,
    doc_id: str = "",
    *,
    service: RagService | None = None,
) -> ParsedDocument:
    """Parse ``path`` into ordered blocks (remote first, local fallback)."""
    path = Path(path)
    doc_id = doc_id or path.stem
    service = service or get_rag_service()

    remote_error: Exception | None = None
    if service.available:
        try:
            parsed = service.parse(path, doc_id)
            document = _from_remote(parsed, doc_id)
            if document is not None:
                logger.info("外规解析(远端): %s → %d blocks", path.name, len(document.blocks))
                return document
            remote_error = ValueError("远端解析结果为空")
        except RagError as exc:
            remote_error = exc
            logger.warning("远端解析失败(%s)，尝试本地抽取", exc)
    else:
        remote_error = RagError("RAG_API_BASE 未配置")

    if PARSE_FALLBACK != "local":
        raise ValueError(f"远端解析不可用且已禁用本地回退: {remote_error}")

    document = _from_local(path, doc_id)
    logger.info("外规解析(本地): %s → %d blocks", path.name, len(document.blocks))
    return document


def _from_remote(parsed, doc_id: str) -> ParsedDocument | None:
    """ParsedDoc → blocks; markdown wins over plain text when both exist."""
    if parsed.blocks:
        blocks = blocks_from_remote(parsed.blocks, doc_id)
        if blocks:
            return ParsedDocument(
                doc_id=doc_id, blocks=blocks,
                text=normalize_text(parsed.text or parsed.markdown), source="remote",
            )
    if parsed.markdown.strip():
        markdown = normalize_text(parsed.markdown)
        return ParsedDocument(
            doc_id=doc_id, blocks=blocks_from_markdown(markdown),
            text=markdown, source="remote",
        )
    if parsed.text.strip():
        text = normalize_text(parsed.text)
        return ParsedDocument(
            doc_id=doc_id, blocks=blocks_from_text(text), text=text, source="remote",
        )
    return None


def _from_local(path: Path, doc_id: str) -> ParsedDocument:
    raw = extract_local_text(path)
    text = normalize_text(raw)
    blocks = (
        blocks_from_markdown(text)
        if path.suffix.lower() in (".md", ".markdown")
        else blocks_from_text(text)
    )
    if not blocks:
        raise ValueError(f"文档 {path.name} 解析后没有任何文本")
    return ParsedDocument(doc_id=doc_id, blocks=blocks, text=text, source="local")
