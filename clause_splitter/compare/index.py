"""内规库 → 条款级 DocumentStore + 三种检索索引.

检索单元 = 一条内规条款(第X条原文)。内规库拆条后,每条条款 = ``DocumentStore``
的一个 ``Chunk``:

    chunk_id = "{制度名}#{条款序号}"      (如 "网络数据安全管理条例#第一条")
    meta     = {"doc": 制度名, "clause_number": "第一条", "section": "第三章 数据安全"}

数据来源优先级(每个文档):
1. ``{stem}_structure.json``  —— 新拆分产物,含条款 full_text + parent_section。
2. ``{stem}_条款拆分结果.json``—— 新拆分产物(ClauseSplitResult),按 clause_number
   归并 source_text 重建条款文本。
3. ``{stem}_split_result.json``—— 旧拆分产物,同上回退。

三种索引全部落在 ``doc_cache_dir(store.cache_key())`` 下,``cache_key()`` 由
``corpus_signature``(内容 hash)决定 —— 内容不变即复用,变化自动重建。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clause_splitter.config import EMBED_DEVICE, EMBED_MODEL, doc_cache_dir
from clause_splitter.utils import get_logger
from ._sources import (
    clause_from_structure_file,
    clause_from_split_result_file,
    scan_clause_source_files,
)
from ..tools.searcher import Chunk, DocumentStore, Section, embed_store
from ..tools.searcher.bm25_search import BM25Searcher
from ..tools.searcher.embed import Embedder

logger = get_logger()


# ═══════════════════════════════════════════════════════════════════════
# 构建条款级 store
# ═══════════════════════════════════════════════════════════════════════


def build_clause_store(
    internal_dir: str | Path | None = None,
    internal_obligations: list[dict[str, Any]] | None = None,
    name: str = "internal",
) -> DocumentStore:
    """把内规库组装成条款级 DocumentStore(每条条款一个 Chunk)。

    Args:
        internal_dir: 扫描该目录下每份制度的 structure.json / 拆分结果 JSON。
        internal_obligations: 直接传入的义务记录列表(按 clause_number 归并)。
        name: store 名,决定缓存目录 ``.cache/<name>/``。

    Returns:
        DocumentStore;``chunks`` 按文档插入顺序、文档内条款按原文顺序。
    """
    store = DocumentStore(name=name)
    # doc_id -> already-added set,避免同一制度多来源重复入 store。
    seen_docs: dict[str, set[str]] = {}

    if internal_obligations:
        _add_obligation_clauses(store, internal_obligations, seen_docs)

    if internal_dir:
        internal_dir = Path(internal_dir)
        if not internal_dir.is_dir():
            logger.warning("internal_dir 不存在,跳过扫描: %s", internal_dir)
            return store
        for source_file in scan_clause_source_files(internal_dir):
            _add_source_file(store, source_file, seen_docs)

    logger.info(
        "条款级 store 就绪: %d 制度, %d 条款", len(store.docs), len(store.all_chunks()),
    )
    return store


def _add_source_file(
    store: DocumentStore,
    source_file: Path,
    seen_docs: dict[str, set[str]],
) -> None:
    """把一份制度的 structure.json / 拆分结果并入 store。"""
    name = source_file.name
    try:
        if name.endswith("_structure.json"):
            clause_texts = clause_from_structure_file(source_file)
        else:
            clause_texts = clause_from_split_result_file(source_file)
    except Exception as e:  # noqa: BLE001 — 单文件失败不拖垮整个库
        logger.warning("解析 %s 失败,跳过: %s", source_file, e)
        return

    _add_clause_texts(store, clause_texts, seen_docs)


def _add_obligation_clauses(
    store: DocumentStore,
    obligations: list[dict[str, Any]],
    seen_docs: dict[str, set[str]],
) -> None:
    """按 clause_number 归并义务 source_text 重建条款文本。"""
    grouped: dict[str, dict[str, Any]] = {}
    for obl in obligations:
        doc = str(obl.get("document_name", "") or "").strip() or "未命名制度"
        clause = str(obl.get("clause_number", "") or "").strip() or "全文"
        key = f"{doc}||{clause}"
        entry = grouped.setdefault(
            key, {"doc": doc, "clause": clause, "texts": [], "section": None}
        )
        src = str(obl.get("source_text", "") or "").strip()
        if src and src not in entry["texts"]:
            entry["texts"].append(src)

    clause_texts = []
    for entry in grouped.values():
        clause_texts.append({
            "doc": entry["doc"],
            "clause_number": entry["clause"],
            "section": entry["section"],
            "full_text": " ".join(entry["texts"]),
        })
    _add_clause_texts(store, clause_texts, seen_docs)


def _add_clause_texts(
    store: DocumentStore,
    clause_texts: list[dict[str, Any]],
    seen_docs: dict[str, set[str]],
) -> None:
    for item in clause_texts:
        doc = str(item.get("doc", "") or "").strip() or "未命名制度"
        clause = str(item.get("clause_number", "") or "").strip() or "全文"
        full_text = str(item.get("full_text", "") or "").strip()
        if not full_text:
            continue
        if clause in seen_docs.get(doc, set()):
            continue  # 更高优先级来源已收录此条款
        seen_docs.setdefault(doc, set()).add(clause)

        chunk_id = f"{doc}#{clause}"
        section = str(item.get("section") or "") or doc
        store.add_chunk(Chunk(
            chunk_id=chunk_id,
            doc_id=doc,
            order=len(store.docs.get(doc, [])),
            text=full_text,
            meta={"doc": doc, "clause_number": clause, "section": section},
        ))


# ═══════════════════════════════════════════════════════════════════════
# 构建全部索引
# ═══════════════════════════════════════════════════════════════════════


def build_all_indexes(
    store: DocumentStore,
    *,
    embed_model: str | None = None,
    rebuild: bool = False,
) -> dict[str, Any]:
    """为 store 构建三种检索索引 + 持久化 store。

    - grep:  ``store.materialize()`` → ``.cache/<key>/chunks/`` + manifest
    - bm25:  ``BM25Searcher(store)``  → ``.cache/<key>/bm25/``(jieba 预分词)
    - faiss: ``embed_store(store)``   → ``.cache/<key>/faiss/``(embedding 模型
      缺失/失败时跳过,不阻塞 grep/bm25)
    - store: ``store.to_jsonl()``     → ``.cache/<key>/store/``(比对热加载)

    Args:
        store: 条款级 DocumentStore。
        embed_model: embedding 模型路径;默认 ``EMBED_MODEL``。
        rebuild: 强制重建全部索引。

    Returns:
        各索引目录路径的摘要 dict。
    """
    cache_root = doc_cache_dir(store.cache_key())
    cache_root.mkdir(parents=True, exist_ok=True)

    # 1) grep —— 物化为每条款一个 .txt + manifest。
    #    materialize() 返回 manifest dict;目录路径用 doc_cache_dir 推算。
    chunks_dir = doc_cache_dir(store.cache_key()) / "chunks"
    store.materialize(chunks_dir)

    # 2) bm25 —— jieba 预分词 Lucene 索引。
    BM25Searcher(store, rebuild=rebuild)

    # 3) faiss —— 语义向量索引(模型缺失时不阻塞)。
    faiss_dir = None
    try:
        embedder = Embedder(
            model_name=embed_model or EMBED_MODEL, device=EMBED_DEVICE
        )
        faiss_dir = embed_store(store, embedder, rebuild=rebuild)
    except Exception as e:  # noqa: BLE001
        logger.warning("faiss 索引跳过(embedding 不可用): %s", e)

    # 4) 持久化 store,供 run_compare 热加载。
    store_dir = cache_root / "store"
    store.to_jsonl(store_dir)

    summary = {
        "cache_key": store.cache_key(),
        "cache_root": str(cache_root),
        "chunks": str(chunks_dir),
        "bm25": str(cache_root / "bm25"),
        "faiss": str(faiss_dir) if faiss_dir else None,
        "store": str(store_dir),
        "documents": len(store.docs),
        "clauses": len(store.all_chunks()),
    }
    logger.info("索引构建完成: %s", summary)
    return summary
