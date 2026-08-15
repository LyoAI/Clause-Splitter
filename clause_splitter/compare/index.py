"""内规库数据准备 —— mineru 解析 + 层级分块 → 检索单元 + 三种索引.

数据源是内规库的**原始文档**(pdf / docx / pptx / xlsx 多制度容器 / 图片)。
流程照搬 FinIndicator ``run_prepare_docs.py`` 的 ``prepare_docs``:

    parse  MinerU 本地 CLI → blocks(.xlsx 走 ``split_workbook`` 拆成每制度一个文档)
    chunk  层级感知分块 → 检索单元(统一按条:每条第X条一个 chunk,章节进 meta.section)
    store  DocumentStore → JSONL(``store/``) + 每块 .txt(``chunks/``) + 源清单(``sources.json``)
    embed  chunks → faiss 向量索引(``faiss/``)
    index  chunks → BM25 Lucene 索引(``bm25/``)

**增量更新**:``prepare_docs`` 读上一次的 ``store/sources.json``,与当前目录逐文件
sha256 比对,分出 added / modified / unchanged / removed:

- 只解析 added+modified(mineru 的 ``skip_existing`` 复用 unchanged 的 md 缓存;
  modified 先删除其旧 md 缓存再重解析)。re-chunk 全部现文档,廉价无 LLM/OCR。
- faiss:纯新增(无 modified/removed)→ 读旧索引 append 新向量;有 modified/removed
  → 全量重嵌入。
- bm25:有变更 → 从全量 store 重建(~1s);无变更 → 复用缓存。
- ``rebuild=True`` 强制全量:删除全部 md 缓存 + 全量重嵌入。

**自定义存储位置**:``index_root``(或 ``INDEX_CACHE_DIR`` / CLI ``--index-dir``)
决定 ``<root>/<name>/{store,chunks,bm25,faiss,md}``;``store/meta.json`` 记录
``index_root``,``load_store`` 供比对侧直接装载既有索引,无需重复 mineru 解析。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from clause_splitter.config import (
    EMBED_DEVICE,
    EMBED_MODEL,
    MINERU_MD_DIRNAME,
    doc_cache_dir,
)
from clause_splitter.utils import get_logger
from ..tools.parser.ingest import ingest, scan_supported_docs
from ..tools.searcher import DocumentStore, embed_store
from ..tools.searcher.bm25_search import BM25Searcher
from ..tools.searcher.embed import Embedder

logger = get_logger()


def _scan_sources(internal_dir: str | Path) -> dict[Path, str]:
    """{source file: sha256} for every supported document under ``internal_dir``."""
    from ..tools.parser.ingest import _file_sha256

    return {p: _file_sha256(p) for p in scan_supported_docs(internal_dir)}


def _load_sources(cache_root: Path) -> dict[str, dict]:
    """The persisted ``store/sources.json``, or {} when absent / unreadable."""
    f = cache_root / "store" / "sources.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt manifest is treated as empty
            logger.warning("sources.json 无法解析,视为空: %s", f)
    return {}


def prepare_docs(
    internal_dir: str | Path,
    *,
    name: str = "internal",
    embed_model: str | None = None,
    rebuild: bool = False,
    persist: bool = True,
    index_root: str | Path | None = None,
) -> DocumentStore:
    """Run the offline pipeline for the internal library and return the store.

    Idempotent and incremental: unchanged source files reuse their cached
    MinerU parse / faiss rows; only added / modified files are re-parsed.
    ``rebuild=True`` forces a full rebuild (re-parse + re-embed + re-index).

    Args:
        internal_dir: Directory of internal-regulation documents
            (pdf / docx / pptx / xlsx containers / images).
        name: Store / cache identity (``<root>/<name>/``).
        embed_model: Embedding model path; default ``EMBED_MODEL``.
        rebuild: Force-rebuild all derived indexes.
        persist: Gate every disk write — False runs parse → chunk → store in
            memory only and builds no indexes.
        index_root: Cache parent override (default ``INDEX_CACHE_DIR`` env var,
            then ``CACHE_DIR``).

    Returns:
        The populated store (chunks in reading order).
    """
    cache_root = doc_cache_dir(name, root=index_root)
    current = _scan_sources(internal_dir)
    prev = _load_sources(cache_root)

    if rebuild:
        md_dir = cache_root / MINERU_MD_DIRNAME
        if md_dir.exists():
            logger.info("--rebuild: 清除全部 mineru 解析缓存 %s", md_dir)
            shutil.rmtree(md_dir, ignore_errors=True)
        added = list(current)
        modified: list[Path] = []
        removed: list[str] = []
    else:
        prev_hash = {
            s.get("source_file"): s.get("sha256")
            for s in prev.values() if s.get("source_file")
        }
        added = [p for p in current if str(p) not in prev_hash]
        modified = [
            p for p in current
            if str(p) in prev_hash and prev_hash[str(p)] != current[p]
        ]
        removed = [f for f in prev_hash if f not in {str(p) for p in current}]
        # 已变更文件:删掉其旧 md 缓存,``parse_docs(skip_existing)`` 才会重新解析。
        # unchanged 文件缓存直接复用 → 增量只跑新/改文件的 mineru。
        md_dir = cache_root / MINERU_MD_DIRNAME
        for p in modified:
            stale = md_dir / p.stem
            if stale.exists():
                logger.info("文件已变更,清除旧解析缓存: %s", p.name)
                shutil.rmtree(stale, ignore_errors=True)

    changed = bool(added or modified or removed)
    logger.info(
        "增量差分 %s: added=%d modified=%d removed=%d → changed=%s",
        name, len(added), len(modified), len(removed), changed,
    )

    if not persist:
        return ingest(list(current), name=name, persist=False, index_root=index_root)

    store = ingest(list(current), name=name, persist=True, index_root=index_root)

    # faiss:纯新增 append;modified/removed/rebuild → 全量重嵌入(否则旧行残留,
    # 且改过的 chunk_id 会漂移造成重复向量)。
    reembed = rebuild or bool(modified or removed)
    try:
        embed_store(
            store,
            Embedder(model_name=embed_model or EMBED_MODEL, device=EMBED_DEVICE),
            index_dir=cache_root / "faiss",
            rebuild=reembed,
            append_new=(not reembed and bool(added)),
        )
    except Exception as e:  # noqa: BLE001 — 模型缺失不阻塞 grep/bm25
        logger.warning("faiss 索引跳过(embedding 不可用): %s", e)

    BM25Searcher(store, index_dir=cache_root / "bm25", rebuild=changed)
    return store


def load_store(
    name: str = "internal",
    index_root: str | Path | None = None,
) -> DocumentStore | None:
    """Load a previously built store + its persisted provenance, or None.

    Only the serialized store is read — no MinerU parse, no index rebuild — so
    ``run_compare`` can reuse an existing internal-library index directly. The
    store's ``index_root`` comes from ``store/meta.json``, keeping the faiss /
    bm25 / chunks locations aligned with whatever ``run_index --index-dir`` used.

    Args:
        name: Store identity (``<root>/<name>/``).
        index_root: Where to look for the store; default ``CACHE_DIR`` /
            ``INDEX_CACHE_DIR``. If a store exists there, its persisted
            ``index_root`` wins for locating the derived indexes.
    """
    cache_root = doc_cache_dir(name, root=index_root)
    store_dir = cache_root / "store"
    if not (store_dir / "chunks.jsonl").exists():
        return None
    store = DocumentStore.load_from_jsonl(store_dir)
    logger.info("已装载既有内规索引库 %s (%d 文档 / %d 检索单元)", name, len(store.docs), len(store.all_chunks()))
    return store
