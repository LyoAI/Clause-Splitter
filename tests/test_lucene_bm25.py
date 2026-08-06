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

        # Test 3: No-match query → 0 hits (jieba word-level precision).
        # With unigram this returned spurious low scores; word-level matching
        # correctly rejects unrelated queries like "金融衍生品".
        results = searcher.search("金融衍生品交易风险管理", k=3)
        assert len(results) == 0, (
            f"Unrelated query should match nothing, got {len(results)} hits"
        )
        print("  ✓ Search '金融衍生品' → 0 hits (word-level precision)")

        # Test 4: get_by_id (instant from store, no Lucene I/O).
        # Ids are composite ({document_name}::{index}) to avoid collisions.
        detail = searcher.get_by_id("制度A::1")
        assert detail is not None
        assert detail["index"] == 1
        assert detail["document_name"] == "制度A"
        print(f"  ✓ get_by_id('制度A::1') → {detail['document_name']} #{detail['index']}")

        # Non-existent ID
        detail = searcher.get_by_id("999")
        assert detail is None
        print(f"  ✓ get_by_id('999') → None (correct)")

        # Test 5: Larger k
        results = searcher.search("应当", k=10)
        assert len(results) >= 3  # 3 of 5: doc 4 uses 必须 not 应当
        print(f"  ✓ Search '应当' k=10 → {len(results)} hits")

        # Test 6: manifest records the analyzer version (drives smart-append)
        from tools.search.index_manager import IndexManager
        from tools.search.tokenizer import ANALYZER_VERSION
        manifest = IndexManager._read_manifest(index_path)
        assert manifest.get("analyzer") == ANALYZER_VERSION
        print(f"  ✓ Manifest analyzer: {manifest['analyzer']}")

        searcher.close()

        print("\n✓ All BM25 IndexManager + BM25Searcher tests passed!")

    finally:
        if 'mgr' in dir():
            mgr.cleanup()
        shutil.rmtree(index_dir, ignore_errors=True)


def test_analyzer_mismatch_safety():
    """Analyzer mismatch must never silently mix tokenizers in one index:
    a direct append raises (it only has the new docs), while
    build_from_directory (which re-scans the full set) rebuilds safely."""
    import json
    import tempfile
    import shutil
    from pathlib import Path

    import pytest

    from tools.search.index_manager import IndexManager
    from tools.search.tokenizer import ANALYZER_VERSION

    index_dir = tempfile.mkdtemp()
    try:
        doc = lambda i, d, txt: {
            "index": i, "document_name": d, "clause_number": "第1条",
            "subject": "数据处理者", "action_verb": "建立",
            "action_target": "制度", "source_text": txt, "obligation_rule": "应当",
        }

        def _legacy_ize(path: str) -> None:
            """Stamp a mismatched analyzer onto an index's manifest."""
            manifest = IndexManager._read_manifest(path)
            manifest["analyzer"] = "unigram-v0"
            (Path(path) / "index_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        # ── Case 1: direct build(append=True) → must RAISE (protects old docs) ──
        path = IndexManager().build(
            [doc(1, "制度A", "数据处理者应当建立数据安全管理制度。")],
            index_path=index_dir + "/idx",
        )
        _legacy_ize(path)
        with pytest.raises(RuntimeError, match="analyzer"):
            IndexManager().build(
                [doc(2, "制度B", "网络运营者应当制定网络安全应急预案。")],
                index_path=path, append=True,
            )
        print("  ✓ Direct append on analyzer mismatch → raises (old docs preserved)")

        # ── Case 2: build_from_directory → full rebuild, nothing lost ──
        scan = Path(index_dir) / "internal"
        (scan / "制度A").mkdir(parents=True)
        (scan / "制度B").mkdir()
        (scan / "制度A" / "制度A_split_result.json").write_text(json.dumps(
            {"document_name": "制度A", "obligations": [
                doc(1, "制度A", "数据处理者应当建立数据安全管理制度。")]},
            ensure_ascii=False), encoding="utf-8")
        (scan / "制度B" / "制度B_split_result.json").write_text(json.dumps(
            {"document_name": "制度B", "obligations": [
                doc(1, "制度B", "网络运营者应当制定网络安全应急预案。")]},
            ensure_ascii=False), encoding="utf-8")

        mgr = IndexManager()
        path2 = mgr.build_from_directory(str(scan), append=True)
        new_manifest = IndexManager._read_manifest(path2)
        assert new_manifest.get("analyzer") == ANALYZER_VERSION
        assert new_manifest["total_obligations"] == 2, "full rebuild must keep BOTH docs"
        print("  ✓ build_from_directory on analyzer mismatch → rebuild, 2/2 docs kept")

    finally:
        shutil.rmtree(index_dir, ignore_errors=True)


def test_persisted_store_sidecar():
    """build_from_directory persists the obligation store so repeated
    comparisons can serve 原文数据 without re-uploading the library."""
    import json
    import tempfile
    import shutil
    from pathlib import Path

    from tools.search.index_manager import IndexManager, STORE_FILE

    tmp = tempfile.mkdtemp()
    IndexManager.clear_store_cache()
    try:
        scan = Path(tmp) / "internal"
        (scan / "制度A").mkdir(parents=True)
        (scan / "制度A" / "制度A_split_result.json").write_text(json.dumps(
            {"document_name": "制度A", "obligations": [
                {"index": 1, "document_name": "制度A", "clause_number": "第一条",
                 "subject": "数据处理者", "action_verb": "建立",
                 "action_target": "数据安全管理制度", "obligation_rule": "应当",
                 "source_text": "数据处理者应当建立数据安全管理制度。"},
                {"index": 2, "document_name": "制度A", "clause_number": "第二条",
                 "subject": "网络运营者", "action_verb": "制定",
                 "action_target": "应急预案", "obligation_rule": "应当",
                 "source_text": "网络运营者应当制定网络安全应急预案。"},
            ]},
            ensure_ascii=False), encoding="utf-8")

        path = IndexManager().build_from_directory(str(scan), str(scan / ".bm25_index"))
        assert (Path(path) / STORE_FILE).exists(), "sidecar must be persisted"

        store = IndexManager.load_store_from_disk(path)
        assert len(store) == 2
        assert store["制度A::1"]["source_text"] == "数据处理者应当建立数据安全管理制度。"

        obls = IndexManager.load_obligations_from_index(path)
        assert len(obls) == 2
        assert {o["index"] for o in obls} == {1, 2}
        print("  ✓ Sidecar persisted; load_store_from_disk / load_obligations_from_index OK")

    finally:
        IndexManager.clear_store_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def test_append_merges_persisted_store():
    """Appending new docs must merge with the persisted store, never lose it."""
    import json
    import tempfile
    import shutil
    from pathlib import Path

    from tools.search.index_manager import IndexManager

    tmp = tempfile.mkdtemp()
    IndexManager.clear_store_cache()
    try:
        scan = Path(tmp) / "internal"
        (scan / "制度A").mkdir(parents=True)
        (scan / "制度A" / "制度A_split_result.json").write_text(json.dumps(
            {"document_name": "制度A", "obligations": [
                {"index": 1, "document_name": "制度A", "clause_number": "第一条",
                 "subject": "数据处理者", "action_verb": "建立",
                 "action_target": "制度", "obligation_rule": "应当",
                 "source_text": "数据处理者应当建立数据安全管理制度。"},
            ]}, ensure_ascii=False), encoding="utf-8")

        idx = str(scan / ".bm25_index")
        IndexManager().build_from_directory(str(scan), idx)
        assert len(IndexManager.load_store_from_disk(idx)) == 1

        # 制度B arrives later → incremental append
        (scan / "制度B").mkdir()
        (scan / "制度B" / "制度B_split_result.json").write_text(json.dumps(
            {"document_name": "制度B", "obligations": [
                {"index": 1, "document_name": "制度B", "clause_number": "第一条",
                 "subject": "网络运营者", "action_verb": "制定",
                 "action_target": "预案", "obligation_rule": "应当",
                 "source_text": "网络运营者应当制定网络安全应急预案。"},
            ]}, ensure_ascii=False), encoding="utf-8")
        IndexManager().build_from_directory(str(scan), idx, append=True)

        store = IndexManager.load_store_from_disk(idx)
        assert len(store) == 2, f"append must merge, got {len(store)}"
        assert "制度A::1" in store and "制度B::1" in store
        print("  ✓ Append merges with persisted store (制度A + 制度B = 2 entries)")
    finally:
        IndexManager.clear_store_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def test_pipeline_search_space_honors_passed_library():
    """The persistent index is only an accelerator: a caller-provided library
    that differs from the index must NOT be searched through the stale index
    (this was the root cause of mass '缺失' when a fresh library was uploaded).
    """
    import json
    import tempfile
    import shutil
    from pathlib import Path

    from tools.search.index_manager import IndexManager
    from pipeline.compare_pipeline import _index_matches_library

    def _obl(i, doc, txt):
        return {"index": i, "document_name": doc, "clause_number": "第1条",
                "subject": "数据处理者", "action_verb": "建立",
                "action_target": "制度", "obligation_rule": "应当",
                "source_text": txt}

    tmp = tempfile.mkdtemp()
    IndexManager.clear_store_cache()
    try:
        scan = Path(tmp) / "internal"
        (scan / "制度A").mkdir(parents=True)
        (scan / "制度A" / "制度A_split_result.json").write_text(json.dumps(
            {"document_name": "制度A", "obligations": [
                _obl(1, "制度A", "数据处理者应当建立数据安全管理制度。"),
                _obl(2, "制度A", "网络运营者应当制定网络安全应急预案。"),
            ]}, ensure_ascii=False), encoding="utf-8")
        idx = str(scan / ".bm25_index")
        IndexManager().build_from_directory(str(scan), idx)

        # Exact same library → fast path (reuse index)
        same = [_obl(1, "制度A", "数据处理者应当建立数据安全管理制度。"),
                _obl(2, "制度A", "网络运营者应当制定网络安全应急预案。")]
        assert _index_matches_library(idx, same) is True

        # Different library (new doc / different set) → must NOT reuse index
        other = [_obl(1, "制度B", "网络运营者应当制定网络安全应急预案。")]
        assert _index_matches_library(idx, other) is False

        # Empty library → never reuse
        assert _index_matches_library(idx, []) is False

        # Same count but different content → must NOT reuse
        swapped = [_obl(1, "制度A", "网络运营者应当制定网络安全应急预案。"),
                   _obl(2, "制度A", "数据处理者应当建立数据安全管理制度。")]
        assert _index_matches_library(idx, swapped) is False
        print("  ✓ Search-space gating: reuse index only when library matches exactly")
    finally:
        IndexManager.clear_store_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def test_store_jsonl_format_and_legacy_migration():
    """The persisted store is JSONL (one obligation per line, each carrying an
    explicit 'id'); a legacy single-file obligations.json is still readable and
    is migrated away on the next build."""
    import json
    import tempfile
    import shutil
    from pathlib import Path

    from tools.search.index_manager import (
        IndexManager, STORE_FILE, LEGACY_STORE_FILE,
    )

    tmp = tempfile.mkdtemp()
    IndexManager.clear_store_cache()
    try:
        idx = str(Path(tmp) / "idx")
        mgr = IndexManager()
        path = mgr.build(
            [{"index": 1, "document_name": "制度A", "clause_number": "第1条",
              "subject": "数据处理者", "action_verb": "建立",
              "action_target": "制度", "obligation_rule": "应当",
              "source_text": "数据处理者应当建立数据安全管理制度。"}],
            index_path=idx,
        )
        jl = Path(path) / STORE_FILE
        assert jl.exists(), "store must be written as JSONL"
        lines = [l for l in jl.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["id"] == "制度A::1", "each line must carry the composite id"
        assert rec["source_text"].endswith("。")

        # Legacy single-file JSON is still readable (backward compat) when no
        # JSONL exists yet — i.e. an index built before the JSONL switch.
        jl.unlink()  # simulate a pre-JSONL index
        legacy = Path(path) / LEGACY_STORE_FILE
        legacy.write_text(json.dumps({"制度A::1": {
            "index": 1, "document_name": "制度A", "source_text": "旧格式原文",
        }}, ensure_ascii=False), encoding="utf-8")
        IndexManager.clear_store_cache()  # fresh process: no warm cache
        store = IndexManager.load_store_from_disk(path)
        assert store["制度A::1"]["source_text"] == "旧格式原文"
        # ...but a rebuild migrates to JSONL and drops the legacy file.
        mgr2 = IndexManager()
        mgr2._index_path = path
        mgr2.load_obligation_store([{"index": 1, "document_name": "制度A",
                                     "source_text": "数据处理者应当建立数据安全管理制度。"}])
        mgr2._write_store_file(path)
        assert not legacy.exists(), "legacy file must be removed after migration"
        assert (Path(path) / STORE_FILE).exists()
        print("  ✓ Store is JSONL; legacy obligations.json readable + migrated")
    finally:
        IndexManager.clear_store_cache()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_index_manager_build_and_search()
    test_analyzer_mismatch_safety()
    test_persisted_store_sidecar()
    test_append_merges_persisted_store()
    test_pipeline_search_space_honors_passed_library()
    test_store_jsonl_format_and_legacy_migration()
