"""Integration test: 新 BM25Searcher(jieba 预分词 Lucene)构建 + 检索.

验证技术关键点:索引用 ``-pretokenized``(jieba 词空格连接),查询走
``WhitespaceAnalyzer`` + ``to_pretokenized(query)``,两侧分词一致才命中中文。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from clause_splitter.tools.searcher.store import Chunk, DocumentStore
from clause_splitter.tools.searcher.tokenizer import ANALYZER_VERSION


def _mini_store() -> DocumentStore:
    """三条款小库,供索引构建。"""
    store = DocumentStore(name="test_bm25")
    docs = [
        ("网络数据安全管理条例#第一条", "数据处理者应当建立数据安全管理制度。"),
        ("网络数据安全管理条例#第二条", "禁止未经授权的数据访问和数据泄露。"),
        ("网络数据安全管理条例#第三条", "应当定期进行安全风险评估和审计。"),
        ("网络数据安全管理条例#第四条", "必须建立个人信息保护影响评估制度。"),
    ]
    for i, (cid, text) in enumerate(docs):
        doc, clause = cid.split("#", 1)
        store.add_chunk(Chunk(
            chunk_id=cid, doc_id=doc, order=i, text=text,
            meta={"doc": doc, "clause_number": clause, "section": "测试章节"},
        ))
    return store


def test_bm25_jieba_pretokenized_hits_chinese_query():
    """jieba 预分词:中文多词查询应命中,而非被切单字返回 0。"""
    store = _mini_store()
    with tempfile.TemporaryDirectory() as td:
        from clause_splitter.tools.searcher.bm25_search import BM25Searcher

        searcher = BM25Searcher(store, index_dir=Path(td) / "bm25")
        # 查询侧必须与索引侧同一 to_pretokenized,否则中文被切单字 → 0 命中。
        hits = searcher.search("数据安全风险评估", k=5)
        ids = [h.chunk_id for h in hits]
        assert "网络数据安全管理条例#第三条" in ids, f"风险评估条款应命中,got {ids}"
        # 存储命中文本与 store 原文一致。
        assert any("风险评估" in h.text for h in hits)


def test_bm25_index_reuse_and_analyzer_version():
    """同一 index_dir 二次构造复用;tokenizer 版本变化强制重建。"""
    store = _mini_store()
    with tempfile.TemporaryDirectory() as td:
        from clause_splitter.tools.searcher.bm25_search import BM25Searcher

        index_dir = Path(td) / "bm25"
        first = BM25Searcher(store, index_dir=index_dir)
        h1 = first.search("个人信息", k=3)
        # 复用缓存:不重建(不重复 boot JVM 不影响正确性,只要结果一致)。
        second = BM25Searcher(store, index_dir=index_dir)
        h2 = second.search("个人信息", k=3)
        assert [h.chunk_id for h in h1] == [h.chunk_id for h in h2]

        # analyzer_version.txt 记录当前分词器版本。
        assert (index_dir / "analyzer_version.txt").read_text(encoding="utf-8") == ANALYZER_VERSION


def test_bm25_rebuild_forces_reindex():
    """rebuild=True 强制重建,结果一致。"""
    store = _mini_store()
    with tempfile.TemporaryDirectory() as td:
        from clause_splitter.tools.searcher.bm25_search import BM25Searcher

        index_dir = Path(td) / "bm25"
        s1 = BM25Searcher(store, index_dir=index_dir)
        s2 = BM25Searcher(store, index_dir=index_dir, rebuild=True)
        assert [h.chunk_id for h in s1.search("风险评估", k=5)] == [
            h.chunk_id for h in s2.search("风险评估", k=5)
        ]
