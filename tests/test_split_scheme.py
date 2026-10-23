"""Split ladder layers ①–③ — scheme inference, validation, application."""

from __future__ import annotations

import json

from clause_splitter.compare.headings import infer_regulation_headings
from clause_splitter.compare.loader import clauses_from_blocks
from clause_splitter.llm.provider.base import LLMResponse
from clause_splitter.pipeline.split_stage import clauses_from_file
from clause_splitter.split import scheme
from clause_splitter.split.scheme import (
    apply_scheme,
    blocks_from_text,
    infer_scheme_llm,
    sanitize_scheme,
)

ENGLISH_DOC = """DATA GOVERNANCE ACT

Part I - General Provisions

Article 1
This Act applies to the processing of personal data by controllers.

Article 2
Controllers shall implement appropriate technical and organisational measures.

Article 3
A record of processing activities shall be maintained and kept up to date.

Article 4
Personal data breaches shall be reported to the authority within 72 hours.
"""

CHINESE_DOC = """测试管理办法
第一章　总　则
第一条　为了规范数据处理活动，制定本办法。
第二条　本办法适用于公司全体部门。
第二章　附　则
第三条　本办法自发布之日起施行。
"""

#: A scheme the builtin 编章节条 regexes cannot express — the point of layer ①.
ENGLISH_SCHEME = json.dumps({"patterns": [
    {"pattern": "Part [IVX]+", "level": 1, "kind": "section"},
    {"pattern": "Article \\d+", "level": 2, "kind": "article"},
]})


class SchemeLLM:
    """Answers only the split_scheme prompt with a canned payload."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    async def ainvoke(self, messages, tools=None, **kwargs):
        assert "编号体系" in messages[0]["content"]
        self.calls += 1
        return LLMResponse(content=self.payload, finish_reason="stop")


def _rules(**overrides):
    item = {"pattern": r"Article \d+", "level": 2, "kind": "article"}
    item.update(overrides)
    return [item]


def test_blocks_from_text_per_line():
    blocks = blocks_from_text("第一条　甲。\n\n第二条　乙。\n")
    assert [b.text for b in blocks] == ["第一条　甲。", "第二条　乙。"]
    assert all(b.type == "paragraph" for b in blocks)


def test_apply_scheme_english_articles():
    """English Part/Article scheme → per-article clauses (builtin can't)."""
    blocks = blocks_from_text(ENGLISH_DOC)
    rules = sanitize_scheme(json.loads(ENGLISH_SCHEME)["patterns"], blocks)
    assert rules, "scheme should pass validation on the English doc"
    clauses = clauses_from_blocks("act", apply_scheme(blocks, rules))
    numbers = [c.clause_number for c in clauses]
    assert {"Article 1", "Article 2", "Article 3", "Article 4"} <= set(numbers)
    first = next(c for c in clauses if c.clause_number == "Article 1")
    assert "personal data by controllers" in first.full_text

    # Contrast: the builtin regexes find no markers here at all.
    assert not any(b.type == "title" for b in infer_regulation_headings(blocks))


def test_sanitize_scheme_hard_gates():
    blocks = blocks_from_text(ENGLISH_DOC)
    cases = [
        "not-a-list",                                # not a list
        [{"pattern": r"(", "level": 1, "kind": "article"}],  # uncompilable
        [{"pattern": r"Article \d+", "level": 9, "kind": "article"}],  # level > CHUNK_LEVEL
        [{"pattern": r"Article \d+", "level": 1, "kind": "chapter"}],  # unknown kind
        [  # no article rule
            {"pattern": r"Part [IVX]+", "level": 1, "kind": "section"},
        ],
        [  # duplicate levels
            {"pattern": r"Part [IVX]+", "level": 2, "kind": "section"},
            {"pattern": r"Article \d+", "level": 2, "kind": "article"},
        ],
        [  # a section deeper than every article
            {"pattern": r"Article \d+", "level": 2, "kind": "article"},
            {"pattern": r"Part [IVX]+", "level": 3, "kind": "section"},
        ],
        [  # article pattern never hits this doc
            {"pattern": r"§\d+", "level": 2, "kind": "article"},
        ],
    ]
    for raw in cases:
        assert sanitize_scheme(raw, blocks) is None, raw


async def test_infer_scheme_llm_paths():
    blocks = blocks_from_text(ENGLISH_DOC)
    llm = SchemeLLM(ENGLISH_SCHEME)
    rules = await infer_scheme_llm(blocks, llm)
    assert llm.calls == 1 and rules and rules[-1].kind == "article"

    assert await infer_scheme_llm(blocks, SchemeLLM("garbage not json")) is None
    assert await infer_scheme_llm(blocks, SchemeLLM('{"patterns": []}')) is None

    class Boom(SchemeLLM):
        async def ainvoke(self, messages, tools=None, **kwargs):
            raise RuntimeError("llm down")

    assert await infer_scheme_llm(blocks, Boom("")) is None
    assert await infer_scheme_llm(blocks, None) is None  # no LLM configured


async def test_ladder_llm_scheme_layer(tmp_path):
    src = tmp_path / "act.txt"
    src.write_text(ENGLISH_DOC, encoding="utf-8")
    ladder = await clauses_from_file(src, SchemeLLM(ENGLISH_SCHEME))
    assert ladder is not None
    clauses, layer, scheme_json = ladder
    assert layer == "llm_scheme"
    assert {"Article 1", "Article 4"} <= {c.clause_number for c in clauses}
    assert scheme_json[0]["pattern"] == "Part [IVX]+"


async def test_ladder_falls_to_builtin_for_chinese(tmp_path):
    src = tmp_path / "办法.txt"
    src.write_text(CHINESE_DOC, encoding="utf-8")
    # LLM refuses to propose anything → builtin 编章节条 scheme must win.
    ladder = await clauses_from_file(src, SchemeLLM('{"patterns": []}'))
    assert ladder is not None
    clauses, layer, _scheme = ladder
    assert layer == "builtin"
    assert {"第一条", "第二条", "第三条"} <= {c.clause_number for c in clauses}


async def test_ladder_returns_none_when_no_scheme_fits(tmp_path):
    src = tmp_path / "act.txt"
    src.write_text(ENGLISH_DOC, encoding="utf-8")
    assert await clauses_from_file(src, SchemeLLM('{"patterns": []}')) is None
