"""Tests for the deterministic retrieval layer + the BM25 searcher's store guard.

The compare flow is now retrieve-then-judge: for every external obligation,
``retrieve_candidates`` returns the top-K internal candidates (no LLM). These
tests verify query building, cross-query merge/dedupe, that an identical
obligation retrieves itself (the same-vs-same invariant that the ReAct batch
agent used to fail), and that the searcher still drops store-mismatch hits.
"""
import json
import shutil
import tempfile


def _obligation(index, doc, clause, source, subject="数据处理者",
                action_verb="建立", action_target="数据安全管理制度",
                rule="应当", method="/"):
    return {
        "index": index, "document_name": doc, "clause_number": clause,
        "subject": subject, "action_verb": action_verb,
        "action_target": action_target, "obligation_rule": rule,
        "method": method, "source_text": source,
        "trigger_condition": "/", "trigger_sub_condition": "/",
        "object": "/", "ambiguity_note": "无歧义", "ambiguity_detail": "",
        "should_split": "是",
    }


def _build_index(obligations, index_dir):
    """Build an index and return a searcher whose store == the full set."""
    from tools.search.index_manager import IndexManager
    mgr = IndexManager()
    path = mgr.build(obligations, index_path=index_dir + "/idx")
    searcher = mgr.open()
    return mgr, searcher


def test_build_retrieval_queries():
    """Structured fields + full 原文 become distinct queries."""
    from operators.compare.retrieval import build_retrieval_queries

    obl = _obligation(1, "制度A", "第一条",
                      "数据处理者应当建立数据安全管理制度。",
                      subject="数据处理者", action_verb="建立",
                      action_target="数据安全管理制度")
    queries = build_retrieval_queries(obl)
    assert "数据处理者 建立 数据安全管理制度" in queries
    assert "数据处理者应当建立数据安全管理制度。" in queries
    # Long source text gains a shortened head query
    long_obl = _obligation(2, "制度A", "第二条", "长" * 300)
    queries_long = build_retrieval_queries(long_obl)
    assert len(queries_long) == 3  # structured + full + head(120)
    assert queries_long[-1] == "长" * 120


def test_retrieval_self_match_same_library():
    """Same-vs-same: each obligation's top candidate must be itself."""
    from operators.compare.retrieval import retrieve_candidates
    from tools.search.index_manager import IndexManager

    index_dir = tempfile.mkdtemp()
    mgr = None
    try:
        obligations = [
            _obligation(1, "制度A", "第一条", "国家鼓励网络数据在各行业、各领域的创新应用。",
                        subject="国家", action_verb="鼓励",
                        action_target="网络数据在各行业、各领域的创新应用"),
            _obligation(2, "制度A", "第二条", "网络数据处理者应当建立健全网络数据安全管理制度。",
                        subject="网络数据处理者", action_verb="建立",
                        action_target="网络数据安全管理制度"),
            _obligation(3, "制度A", "第三条", "网络数据处理者应当制定网络安全事件应急预案。",
                        subject="网络数据处理者", action_verb="制定",
                        action_target="网络安全事件应急预案"),
        ]
        mgr, searcher = _build_index(obligations, index_dir)
        for obl in obligations:
            candidates = retrieve_candidates(searcher, obl, top_k=5)
            assert candidates, f"obligation #{obl['index']} should retrieve itself"
            top_id = candidates[0]["docid"]
            assert top_id == IndexManager.make_obligation_id(obl), (
                f"#{obl['index']} top candidate was {top_id}, expected self"
            )
            assert candidates[0]["score"] > 0
        searcher.close()
    finally:
        if mgr:
            mgr.cleanup()
        shutil.rmtree(index_dir, ignore_errors=True)


def test_retrieval_merge_dedupe_max_score():
    """Same docid found by multiple queries is kept once, at its max score."""
    from operators.compare.retrieval import build_retrieval_queries, retrieve_candidates

    index_dir = tempfile.mkdtemp()
    mgr = None
    try:
        obligations = [
            _obligation(1, "制度A", "第一条", "数据处理者应当建立数据安全管理制度。",
                        action_verb="建立", action_target="数据安全管理制度"),
            _obligation(2, "制度A", "第二条", "网络运营者应当制定网络安全应急预案。",
                        action_verb="制定", action_target="网络安全应急预案"),
        ]
        mgr, searcher = _build_index(obligations, index_dir)

        # A query that would match both docs; then assert merge keeps unique ids.
        obl = _obligation(3, "制度A", "第三条", "数据处理者应当建立数据安全管理制度。",
                          action_verb="建立", action_target="数据安全管理制度")
        queries = build_retrieval_queries(obl)
        assert len(queries) >= 2
        candidates = retrieve_candidates(searcher, obl, top_k=5)
        ids = [c["docid"] for c in candidates]
        assert len(ids) == len(set(ids)), "dedupe must collapse duplicate docids"
        assert "制度A::1" in ids
        searcher.close()
    finally:
        if mgr:
            mgr.cleanup()
        shutil.rmtree(index_dir, ignore_errors=True)


def test_search_filters_store_mismatch():
    """Hits absent from the obligation store are dropped so the judge only
    ever sees obligations it can read back in full."""
    from tools.search.bm25_searcher import BM25Searcher
    from tools.search.index_manager import IndexManager

    index_dir = tempfile.mkdtemp()
    mgr = None
    try:
        obligations = [
            _obligation(1, "制度A", "第一条", "数据处理者应当建立数据安全管理制度。"),
            _obligation(2, "制度B", "第一条", "网络运营者应当制定网络安全应急预案。"),
        ]
        # Build the FULL index (both docs), but open the searcher with a
        # store containing ONLY 制度A — simulates reusing a persistent index
        # while the caller passed a subset of internal obligations.
        mgr = IndexManager()
        path = mgr.build(obligations, index_path=index_dir + "/idx")
        partial_store = {"制度A::1": obligations[0]}
        searcher = BM25Searcher(path, obligation_store=partial_store)

        # A query that matches both docs should only return the store-backed one
        results = searcher.search("应当 建立 制度", k=10)
        ids = [r["docid"] for r in results]
        assert "制度A::1" in ids
        assert "制度B::2" not in ids, (
            "hit without store data must be filtered, got %s" % ids
        )
        # And every returned result carries full obligation data
        assert all(r["obligation"].get("source_text") for r in results)
        searcher.close()
    finally:
        if mgr:
            mgr.cleanup()
        shutil.rmtree(index_dir, ignore_errors=True)


if __name__ == "__main__":
    test_build_retrieval_queries()
    test_retrieval_self_match_same_library()
    test_retrieval_merge_dedupe_max_score()
    test_search_filters_store_mismatch()
    print("\n✓ All retrieval tests passed!")
