"""Integration test: BM25 index + search via IndexManager + BM25Searcher.

Tests the complete workflow used by ComparePipeline:
1. Build index via IndexManager (Lucene + in-memory store)
2. Search via BM25Searcher (BM25 scoring + store enrichment)
3. get_by_id for instant detail retrieval
"""
import tempfile
import shutil


def test_index_manager_build_and_search():
    """Build index, search, retrieve details."""
    from tools.search.index_manager import IndexManager

    index_dir = tempfile.mkdtemp()

    try:
        # ── Build index ──────────────────────────────────────────
        mgr = IndexManager(base_dir=index_dir)
        obligations = [
            {
                "index": 1, "subject": "数据处理者", "action_verb": "建立",
                "action_target": "数据安全管理制度",
                "source_text": "数据处理者应当建立数据安全管理制度。",
                "document_name": "制度A", "clause_number": "第一条",
                "obligation_rule": "应当",
            },
            {
                "index": 2, "subject": "数据处理者", "action_verb": "禁止",
                "action_target": "数据非法交易",
                "source_text": "禁止未经授权的数据访问和数据泄露。",
                "document_name": "制度A", "clause_number": "第二条",
                "obligation_rule": "禁止",
            },
            {
                "index": 3, "subject": "网络运营者", "action_verb": "进行",
                "action_target": "安全风险评估",
                "source_text": "应当定期进行安全风险评估和审计。",
                "document_name": "制度B", "clause_number": "第一条",
                "obligation_rule": "应当",
            },
            {
                "index": 4, "subject": "个人信息处理者", "action_verb": "建立",
                "action_target": "个人信息保护影响评估",
                "source_text": "必须建立个人信息保护影响评估制度。",
                "document_name": "制度B", "clause_number": "第三条",
                "obligation_rule": "应当",
            },
            {
                "index": 5, "subject": "网络运营者", "action_verb": "制定",
                "action_target": "网络安全应急预案",
                "source_text": "网络运营者应当制定网络安全应急预案。",
                "document_name": "制度C", "clause_number": "第五条",
                "obligation_rule": "应当",
            },
        ]

        index_path = mgr.build(obligations)
        assert index_path, "Index path should not be empty"
        assert mgr.num_docs == 5
        assert mgr.store_size == 5
        print(f"  ✓ Index built: {mgr.num_docs} docs, {mgr.store_size} store entries")

        # ── Open searcher ────────────────────────────────────────
        searcher = mgr.open()
        assert searcher.num_docs == 5

        # Test 1: Exact match search
        results = searcher.search("数据安全管理制度", k=3)
        assert len(results) > 0
        print(f"  ✓ Search '数据安全管理制度' → {len(results)} hits")
        top = results[0]
        assert top["score"] > 0
        # Verify obligation data from store
        obl = top["obligation"]
        assert obl.get("document_name") == "制度A"
        assert obl.get("action_target") == "数据安全管理制度"
        print(f"      Top hit: {obl['document_name']} #{obl['index']} score={top['score']:.4f}")

        # Test 2: Partial match
        results = searcher.search("个人信息保护 评估", k=3)
        assert len(results) > 0
        print(f"  ✓ Search '个人信息保护 评估' → {len(results)} hits")
        assert results[0]["obligation"].get("document_name") == "制度B"

        # Test 3: Low-score no-match
        results = searcher.search("金融衍生品交易风险管理", k=3)
        if len(results) > 0:
            assert results[0]["score"] < 4.0
        print(f"  ✓ Search '金融衍生品' → low scores (max {results[0]['score']:.4f})")

        # Test 4: get_by_id (instant from store, no Lucene I/O)
        detail = searcher.get_by_id("1")
        assert detail is not None
        assert detail["index"] == 1
        assert detail["document_name"] == "制度A"
        print(f"  ✓ get_by_id('1') → {detail['document_name']} #{detail['index']}")

        # Non-existent ID
        detail = searcher.get_by_id("999")
        assert detail is None
        print(f"  ✓ get_by_id('999') → None (correct)")

        # Test 5: Larger k
        results = searcher.search("应当", k=10)
        assert len(results) >= 3  # 3 of 5: doc 4 uses 必须 not 应当
        print(f"  ✓ Search '应当' k=10 → {len(results)} hits")

        searcher.close()

        print("\n✓ All BM25 IndexManager + BM25Searcher tests passed!")

    finally:
        if 'mgr' in dir():
            mgr.cleanup()
        shutil.rmtree(index_dir, ignore_errors=True)


if __name__ == "__main__":
    test_index_manager_build_and_search()
