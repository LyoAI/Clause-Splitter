"""Tests for the per-obligation LLM judge and the grounding/validation layer.

Core guarantees:
- The judge emits a candidate ROW number, never an obligation id — so a
  fabricated id is structurally unrepresentable. parse_judge_response still
  salvages a composite-id answer (dual acceptance) but rejects anything that
  is not a row or a real candidate.
- validation.ground_match maps the row back to real evidence from the store
  and downgrades any ungroundable coverage claim to 缺失, replacing the
  analysis text so a fabricated quote can never leak into the gap report.
"""
import json


def _candidate(docid, doc, index, clause, source, score=5.0):
    return {
        "docid": docid,
        "score": score,
        "obligation": {
            "index": index, "document_name": doc, "clause_number": clause,
            "source_text": source,
            "subject": "网络数据处理者", "action_verb": "建立",
            "action_target": "制度", "obligation_rule": "应当",
            "method": "/", "object": "/",
        },
    }


_CANDIDATES = [
    _candidate("制度A::1", "制度A", 1, "第一条", "网络数据处理者应当建立健全数据安全管理制度。", 8.5),
    _candidate("制度A::2", "制度A", 2, "第二条", "网络数据处理者应当制定网络安全事件应急预案。", 6.2),
]


def test_parse_judge_response_row():
    """A clean row-number answer parses to a validated 1-based index."""
    from operators.compare.judge import parse_judge_response

    content = json.dumps({
        "external_clause_id": "1",
        "status": "完全覆盖",
        "matched_candidate_index": 2,
        "analysis": "内规第二条原文完全一致。",
        "remedy_suggestion": "",
    }, ensure_ascii=False)
    result = parse_judge_response(content, _CANDIDATES)
    assert result is not None
    assert result["status"] == "完全覆盖"
    assert result["matched_candidate_index"] == 2


def test_parse_judge_response_id_string():
    """A composite-id answer (schema slip) is salvaged to its row number."""
    from operators.compare.judge import parse_judge_response

    content = json.dumps({
        "status": "部分覆盖",
        "matched_candidate_index": "制度A::2",
        "analysis": "仅覆盖应急预案部分。",
        "remedy_suggestion": "补充风险评估要求。",
    }, ensure_ascii=False)
    result = parse_judge_response(content, _CANDIDATES)
    assert result["status"] == "部分覆盖"
    assert result["matched_candidate_index"] == 2


def test_parse_judge_response_fabricated_id():
    """An id that is NOT a candidate row / real candidate → None (invalid)."""
    from operators.compare.judge import parse_judge_response

    content = json.dumps({
        "status": "完全覆盖",
        "matched_candidate_index": "数据安全管理制度::第15条",
        "analysis": "内规《数据安全管理制度》第15条承接。",
        "remedy_suggestion": "",
    }, ensure_ascii=False)
    result = parse_judge_response(content, _CANDIDATES)
    assert result is not None
    assert result["matched_candidate_index"] is None  # ungroundable


def test_parse_judge_response_bad_json():
    """Garbage / missing status / out-of-range row → None or safe default."""
    from operators.compare.judge import parse_judge_response

    assert parse_judge_response("", _CANDIDATES) is None
    assert parse_judge_response("不是 JSON", _CANDIDATES) is None
    # Out-of-range row number → None (invalid)
    content = json.dumps({"status": "完全覆盖", "matched_candidate_index": 99},
                         ensure_ascii=False)
    result = parse_judge_response(content, _CANDIDATES)
    assert result["matched_candidate_index"] is None


def test_judge_prompt_candidate_table():
    """The judge user prompt renders every candidate as a numbered row."""
    from prompts.compare.judge import build_judge_user_prompt

    external = {"index": 1, "clause_number": "第一条", "subject": "国家",
                "action_verb": "鼓励", "action_target": "创新应用",
                "obligation_rule": "应当", "source_text": "国家鼓励网络数据创新应用。"}
    prompt = build_judge_user_prompt(external, _CANDIDATES)
    assert "## 外部义务" in prompt
    assert "国家鼓励网络数据创新应用。" in prompt
    assert "| 1 |" in prompt and "| 2 |" in prompt
    assert "网络数据处理者应当制定网络安全事件应急预案。" in prompt


def test_ground_match_covered():
    """A covered claim with a valid row grounds internal evidence verbatim."""
    from operators.compare.validation import ground_match

    external = {"index": 1, "document_name": "外规.docx", "clause_number": "第一条",
                "source_text": "外规原文"}
    judge = {"status": "完全覆盖", "matched_candidate_index": 2,
             "analysis": "承接", "remedy_suggestion": ""}
    m = ground_match(external, judge, _CANDIDATES)
    assert m["status"] == "完全覆盖"
    assert m["matched_internal_id"] == "制度A::2"
    assert m["matched_internal_document_name"] == "制度A"
    assert m["matched_internal_clause_number"] == "第二条"
    assert m["matched_internal_source_text"] == "网络数据处理者应当制定网络安全事件应急预案。"
    assert m["external_document_name"] == "外规.docx"
    assert m["external_source_text"] == "外规原文"


def test_ground_match_downgrade():
    """A coverage claim that cannot be grounded (no valid row) → 缺失, with the
    analysis replaced so a fabricated quote cannot leak into the report."""
    from operators.compare.validation import ground_match

    external = {"index": 1, "clause_number": "第一条", "source_text": "外规原文"}
    judge = {"status": "完全覆盖", "matched_candidate_index": None,
             "analysis": "内规《数据分类分级管理办法》第3条规定：'应当分类分级'…",
             "remedy_suggestion": ""}
    m = ground_match(external, judge, _CANDIDATES)
    assert m["status"] == "缺失"
    assert m["matched_internal_id"] is None
    assert m["matched_internal_source_text"] == ""
    assert "数据分类分级管理办法" not in m["analysis"], "fabricated quote must be neutralized"
    assert "疑似幻觉" in m["analysis"] or "无法对应" in m["analysis"]
    assert m["remedy_suggestion"]


def test_ground_match_missing():
    """A genuine 缺失 passes through with null internal fields."""
    from operators.compare.validation import ground_match

    external = {"index": 7, "clause_number": "第七条", "source_text": "国家促进产业发展。"}
    judge = {"status": "缺失", "matched_candidate_index": None,
             "analysis": "内规无国家层面条款承接。", "remedy_suggestion": "建议补充。"}
    m = ground_match(external, judge, _CANDIDATES)
    assert m["status"] == "缺失"
    assert m["matched_internal_id"] is None
    assert m["analysis"] == "内规无国家层面条款承接。"
    assert m["remedy_suggestion"] == "建议补充。"


if __name__ == "__main__":
    test_parse_judge_response_row()
    test_parse_judge_response_id_string()
    test_parse_judge_response_fabricated_id()
    test_parse_judge_response_bad_json()
    test_judge_prompt_candidate_table()
    test_ground_match_covered()
    test_ground_match_downgrade()
    test_ground_match_missing()
    print("\n✓ All judge/validation tests passed!")
