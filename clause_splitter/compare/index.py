"""内规库数据准备 —— mineru 解析 + 层级分块 → 检索单元 + 三种索引.

数据源是内规库的**原始文档**(pdf / docx / pptx / xlsx / 图片),不再以
拆分 .json 为来源。流程照搬 FinIndicator ``run_prepare_docs.py`` 的
``prepare_docs``:

    parse  MinerU 本地 CLI → blocks
    chunk  层级感知分块 → 检索单元(按章节/条款标题,粒度**粗于**逐条义务)
    store  DocumentStore → JSONL(``store/``) + 每块 .txt(``chunks/``)
    embed  chunks → faiss 向量索引(``faiss/``)
    index  chunks → BM25 Lucene 索引(``bm25/``)

embed / index 阶段幂等:内容不变即复用缓存(``corpus_signature`` 决定缓存目录);
``rebuild=True`` 强制重建。
"""

from __future__ import annotations

from pathlib import Path

from clause_splitter.config import EMBED_DEVICE, EMBED_MODEL
from clause_splitter.utils import get_logger
from ..tools.parser.ingest import ingest_internal
from ..tools.searcher import DocumentStore, embed_store
from ..tools.searcher.bm25_search import BM25Searcher
from ..tools.searcher.embed import Embedder

logger = get_logger()


def prepare_docs(
    internal_dir: str | Path,
    *,
    name: str = "internal",
    embed_model: str | None = None,
    rebuild: bool = False,
    persist: bool = True,
) -> DocumentStore:
    """Run the offline pipeline for the internal library and return the store.

    Args:
        internal_dir: Directory of internal-regulation documents
            (pdf / docx / pptx / xlsx / images).
        name: Store / cache identity (``.cache/<name>/``).
        embed_model: Embedding model path; default ``EMBED_MODEL``.
        rebuild: Force-rebuild all derived indexes.
        persist: Gate every disk write — False runs parse → chunk → store in
            memory only and builds no indexes.

    Returns:
        The populated store (chunks in reading order).
    """
    store = ingest_internal(internal_dir, name=name, persist=persist)
    if not persist:
        return store
    try:
        embed_store(
            store,
            Embedder(model_name=embed_model or EMBED_MODEL, device=EMBED_DEVICE),
            rebuild=rebuild,
        )
    except Exception as e:  # noqa: BLE001 — 模型缺失不阻塞 grep/bm25
        logger.warning("faiss 索引跳过(embedding 不可用): %s", e)
    BM25Searcher(store, rebuild=rebuild)
    return store
