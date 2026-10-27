"""Shared test fakes — in-memory Mongo, mock LLM, deterministic RAG service.

The service-level tests run the real pipeline (episodes / grounding / reports /
stages / runner) against a fake LLM and an in-memory ``RagService``. Production
code has no fake backend; only tests inject one through ``rag._service``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clause_splitter.agent.hints import SEARCH_TOOL
from clause_splitter.llm.provider.base import LLMResponse, ToolCallRequest
from clause_splitter.pipeline.contexts import Repos
from clause_splitter.rag import (
    Chunk,
    CorpusStats,
    DocInfo,
    Hit,
    ParsedDoc,
    RagNotFound,
    RagUnavailable,
)


# ── in-memory Mongo ───────────────────────────────────────────────────────


class FakeCollection:
    """Motor-collection stand-in: equality + ``$in`` queries, sort/skip/limit."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def _match(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, expected in query.items():
            actual = doc.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self.docs[doc["_id"]] = dict(doc)

    async def insert_many(self, docs: list[dict[str, Any]]) -> None:
        for doc in docs:
            self.docs[doc["_id"]] = dict(doc)

    async def find_one(self, query: dict[str, Any], sort: list | None = None) -> dict | None:
        docs = [d for d in self.docs.values() if self._match(d, query)]
        if sort:
            key, direction = sort[0]
            docs.sort(key=_sort_key(key), reverse=direction < 0)
        return dict(docs[0]) if docs else None

    def find(self, query: dict[str, Any], projection: Any = None,
             sort: list | None = None) -> "_Cursor":
        return _Cursor(self, query, sort)

    async def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for d in self.docs.values() if self._match(d, query))

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> Any:
        for doc in self.docs.values():
            if self._match(doc, query):
                doc.update(update.get("$set", {}))
                return type("Result", (), {"modified_count": 1})()
        return type("Result", (), {"modified_count": 0})()

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> Any:
        n = 0
        for doc in self.docs.values():
            if self._match(doc, query):
                doc.update(update.get("$set", {}))
                n += 1
        return type("Result", (), {"modified_count": n})()

    async def delete_one(self, query: dict[str, Any]) -> None:
        for key in [k for k, d in self.docs.items() if self._match(d, query)]:
            self.docs.pop(key)

    async def delete_many(self, query: dict[str, Any]) -> None:
        for key in [k for k, d in self.docs.items() if self._match(d, query)]:
            self.docs.pop(key)

    async def create_index(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        return None


def _sort_key(key: str):
    def inner(doc: dict[str, Any]):
        value = doc.get(key)
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return value

    return inner


class _Cursor:
    def __init__(self, col: FakeCollection, query: dict[str, Any],
                 sort: list | None = None) -> None:
        self._col, self._query = col, query
        self._sort: tuple[str, int] | None = (sort[0] if sort else None)
        self._skip, self._limit = 0, 0

    def sort(self, key: str, direction: int) -> "_Cursor":
        self._sort = (key, direction)
        return self

    def skip(self, n: int) -> "_Cursor":
        self._skip = n
        return self

    def limit(self, n: int) -> "_Cursor":
        self._limit = n
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        docs = [dict(d) for d in self._col.docs.values() if self._col._match(d, self._query)]
        if self._sort:
            key, direction = self._sort
            docs.sort(key=_sort_key(key), reverse=direction < 0)
        docs = docs[self._skip:]
        cap = self._limit or length or 0
        return docs[:cap] if cap else docs


class FakeDb:
    def __init__(self) -> None:
        self.regulations = FakeCollection()
        self.clauses = FakeCollection()
        self.jobs = FakeCollection()
        self.internal_docs = FakeCollection()


def fake_repos() -> Repos:
    from clause_splitter.db import ClauseRepo, InternalDocRepo, JobRepo, RegulationRepo

    db = FakeDb()
    return Repos(
        regulations=RegulationRepo(db),
        clauses=ClauseRepo(db),
        jobs=JobRepo(db),
        internal_docs=InternalDocRepo(db),
    )


@contextmanager
def data_dir(tmp_path: Path):
    """Point CONFIG_APP.data_dir at a temp dir (frozen dataclass → object.__setattr__)."""
    from config.config_app import CONFIG_APP

    old = CONFIG_APP.data_dir
    object.__setattr__(CONFIG_APP, "data_dir", Path(tmp_path))
    try:
        yield
    finally:
        object.__setattr__(CONFIG_APP, "data_dir", old)


# ── deterministic internal-library store ──────────────────────────────────

LIBRARY_DOCS = [
    ("数据安全管理办法", [
        ("第一条", "本制度适用于公司数据分类分级与数据安全风险评估工作。"),
        ("第二条", "公司重要数据应当加密存储，并实施访问控制。"),
        ("第三条", "公司每年至少开展一次数据安全风险评估。"),
        ("第四条", "涉及个人信息的处理应当遵循最小必要原则。"),
    ]),
    ("信息科技外包风险管理规范", [
        ("第一条", "本规范适用于公司与外部服务提供商的信息科技外包活动。"),
        ("第二条", "公司应当对外包服务提供商开展尽职调查与准入评估。"),
        ("第三条", "外包服务应当签订书面合同并明确双方数据安全责任。"),
    ]),
    ("员工考勤管理制度", [
        ("第一条", "员工应当每日打卡考勤，记录出勤情况。"),
        ("第二条", "迟到、早退按照公司考勤管理办法处理。"),
    ]),
]


def build_rag_service() -> "FakeRagService":
    """In-memory corpus over ``LIBRARY_DOCS`` (deterministic substring search)."""
    return FakeRagService()


class FakeRagService:
    """In-memory ``RagService`` for tests.

    Search scores chunks by query bigram hits — deterministic and engine-free.
    ``parse`` raises by default so split exercises the local fallback; set
    ``parse_text`` to simulate a successful remote parse.
    """

    name = "fake"
    available = True

    def __init__(self) -> None:
        self.parse_text = ""
        self.parse_error: Exception | None = RagUnavailable("fake remote parse disabled")
        self.ingest_error: Exception | None = None
        self.ingests: list[tuple[str, str, str]] = []
        self.deletes: list[str] = []
        self.chunks: list[Chunk] = []
        for doc_id, clauses in LIBRARY_DOCS:
            for i, (num, text) in enumerate(clauses):
                self.chunks.append(Chunk(
                    chunk_id=f"{doc_id}#{i}", doc_id=doc_id, order=i,
                    text=f"{num}　{text}",
                    meta={"doc": doc_id, "section": num, "clause_number": num},
                ))
        self._version = "fake-v1"

    # ── parsing ───────────────────────────────────────────────────────

    def parse(self, path: Path, doc_id: str) -> ParsedDoc:
        if self.parse_error is not None and not self.parse_text:
            raise self.parse_error
        return ParsedDoc(doc_id=doc_id, text=self.parse_text)

    # ── corpus ────────────────────────────────────────────────────────

    def ingest(self, path: Path, name: str, doc_id: str = "") -> DocInfo:
        assert path.exists(), path
        if self.ingest_error is not None:
            raise self.ingest_error
        self.ingests.append((name, doc_id, str(path)))
        doc_id = doc_id or path.stem
        return DocInfo(doc_id=doc_id, name=name, chunk_count=1, state="indexed")

    def delete(self, doc_id: str) -> bool:
        if not any(c.doc_id == doc_id for c in self.chunks):
            raise RagNotFound(f"unknown document: {doc_id}")
        self.deletes.append(doc_id)
        self.chunks = [c for c in self.chunks if c.doc_id != doc_id]
        return True

    def fetch_chunks(self, doc_id: str) -> list[Chunk]:
        return [c for c in self.chunks if c.doc_id == doc_id]

    def documents(self) -> list[DocInfo]:
        counts: dict[str, int] = {}
        for chunk in self.chunks:
            counts[chunk.doc_id] = counts.get(chunk.doc_id, 0) + 1
        return [DocInfo(doc_id=d, name=d, chunk_count=n, state="indexed")
                for d, n in sorted(counts.items())]

    def stats(self) -> CorpusStats:
        return CorpusStats(
            doc_count=len(self.documents()), chunk_count=len(self.chunks), version=self._version
        )

    def search(self, query: str, k: int) -> list[Hit]:
        chars = [c for c in query if c.strip() and c not in "，。；、？！（）《》\"'：“”\n\t "]
        tokens = list(dict.fromkeys(
            [a + b for a, b in zip(chars, chars[1:]) if a != b]
        ))
        if not tokens:
            return []
        scored = []
        for c in self.chunks:
            score = sum(1 for w in tokens if w in c.text)
            if score:
                scored.append(Hit(
                    score=float(score), chunk_id=c.chunk_id, text=c.text,
                    doc_id=c.doc_id, section=str(c.meta.get("section", "")),
                    meta=c.meta,
                ))
        scored.sort(key=lambda h: (-(h.score or 0.0), h.chunk_id))
        return scored[:k]


def patch_rag(monkeypatch, service: FakeRagService) -> FakeRagService:
    """Point the process-wide client at ``service`` for the duration of a test."""
    monkeypatch.setattr("clause_splitter.rag._service", service)
    return service


# ── mock LLM ──────────────────────────────────────────────────────────────

# External clauses + per-clause answers (episode JSON doubles as the
# internalization-judgment source via the internalization_* / clause_type keys).
CLAUSE_SPECS = [
    dict(
        clause_number="第一条",
        full_text="金融机构应当建立数据分类分级制度，并实施数据安全管理，同时做好员工数据安全培训。",
        keyword="数据分类分级",
        answer=dict(
            status="完全覆盖",
            matched_internal_clause_ids=["数据安全管理办法#0"],
            evidence={"数据安全管理办法#0": "本制度适用于公司数据分类分级与数据安全风险评估工作。"},
            analysis="内规第一条已明确数据分类分级制度的适用范围，完整承接外规要求。",
            clause_type="义务性条款", internalization_decision="需要内化",
            internalization_reason="使用'应当'设定义务性要求，属数据合规核心领域，需内化。",
            difference_analysis="",
            core_regulatory_requirements="1）、建立数据分类分级制度\n2）、实施数据安全管理",
            remedy_suggestion="",
        ),
    ),
    dict(
        clause_number="第二条",
        full_text="公司应当对重要数据加密存储并实施访问控制，且每半年开展一次数据安全风险评估。",
        keyword="加密存储",
        answer=dict(
            status="部分覆盖",
            matched_internal_clause_ids=["数据安全管理办法#1"],
            evidence={"数据安全管理办法#1": "公司重要数据应当加密存储，并实施访问控制。"},
            analysis="内规已承接加密存储与访问控制，但风险评估频率低于外规要求。",
            clause_type="义务性条款", internalization_decision="需要内化",
            internalization_reason="义务性条款且涉及数据安全核心要求，需内化。",
            difference_analysis="1）、外规要求每半年开展一次风险评估；内规第三条规定每年至少一次，频率更低。",
            core_regulatory_requirements="1）、对重要数据加密存储\n2）、实施访问控制",
            remedy_suggestion="1）、将数据安全风险评估频率调整为每半年一次",
        ),
    ),
    dict(
        clause_number="第三条",
        full_text="公司应当对数据跨境流动进行安全评估，并经监管机构批准后开展跨境数据传输业务。",
        keyword="跨境",
        answer=dict(
            status="缺失",
            matched_internal_clause_ids=[],
            evidence={},
            analysis="内规库中未检索到任何数据跨境流动相关条款，存在制度真空。",
            clause_type="义务性条款", internalization_decision="需要内化",
            internalization_reason="义务性条款明确设定合规义务，且当前无制度覆盖，需新建制度。",
            difference_analysis="",
            core_regulatory_requirements="1）、对数据跨境流动开展安全评估",
            remedy_suggestion="1）、新增《数据跨境传输管理制度》承接跨境评估义务",
        ),
    ),
    dict(
        clause_number="第四条",
        full_text="鼓励公司采用先进技术手段提升数据安全防护能力，建议建设数据安全运营中心。",
        keyword="数据安全",
        answer=dict(
            status="缺失",
            matched_internal_clause_ids=[],
            evidence={},
            analysis="内规暂无数据安全运营中心相关条款。",
            clause_type="指导性条款", internalization_decision="需要内化",
            internalization_reason="指导性条款（'鼓励/建议'），为防控风险建议内化。",
            difference_analysis="",
            core_regulatory_requirements="1）、采用先进技术提升数据安全防护能力",
            remedy_suggestion="1）、新建数据安全运营中心建设与运营条款",
        ),
    ),
    dict(
        clause_number="第五条",
        full_text="本办法自2026年1月1日起施行，由金融监督管理机构负责解释。",
        keyword="施行",
        answer=dict(
            status="缺失",
            matched_internal_clause_ids=[],
            evidence={},
            analysis="宣示性条款，无需承接。",
            clause_type="宣示性条款", internalization_decision="无需内化",
            internalization_reason="宣示性/程序性条款（生效日期与解释机关），无需内化为公司制度。",
            difference_analysis="",
            core_regulatory_requirements="",
            remedy_suggestion="",
        ),
    ),
]


def spec_by_clause_text(text: str) -> dict:
    for spec in CLAUSE_SPECS:
        if spec["full_text"] in text:
            return spec
    raise RuntimeError(f"no spec for clause text: {text[:60]}")


class PipelineFakeLLM:
    """Dispatches by system prompt: applicability / doc_profile / internalization / episode.

    ``invoke`` raises so split's LLM fallback degrades to the builtin path.
    """

    def __init__(self, applicable: bool | None = True) -> None:
        self.applicable = applicable
        self.calls: list[str] = []

    async def ainvoke(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        system = messages[0]["content"]
        user = next(m["content"] for m in messages if m["role"] == "user")
        if "是否适用于平安科技" in system:
            self.calls.append("applicability")
            if self.applicable is None:
                return LLMResponse(content="抱歉,服务暂时不可用", finish_reason="stop")
            return LLMResponse(
                content=json.dumps({
                    "applicable": self.applicable,
                    "confidence": 0.9,
                    "reason": "mock 判定",
                }, ensure_ascii=False),
                finish_reason="stop",
            )
        if "文档摘要与关键词" in system:
            self.calls.append("doc_profile")
            return LLMResponse(
                content=json.dumps({
                    "abstract": "mock 摘要:本法规规范数据安全管理与风险评估要求。",
                    "keywords": ["数据安全", "风险管理"],
                }, ensure_ascii=False),
                finish_reason="stop",
            )
        if "内化判定框架" in system:
            self.calls.append("internalization")
            spec = spec_by_clause_text(user)
            return LLMResponse(content=json.dumps({
                "clause_type": spec["answer"]["clause_type"],
                "internalization_decision": spec["answer"]["internalization_decision"],
                "internalization_reason": spec["answer"]["internalization_reason"],
            }, ensure_ascii=False), finish_reason="stop")
        self.calls.append("episode")
        return self._episode(system, user, messages, tools)

    def _episode(self, system: str, user: str, messages: list[dict], tools: list[dict] | None):
        if not tools:
            return LLMResponse(content="请按答案契约输出", finish_reason="stop")
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if not has_tool_result:
            spec = spec_by_clause_text(user)
            return LLMResponse(
                content="需要检索内规条款库确认承接情况。",
                tool_calls=[ToolCallRequest(id="call_mock_1", name=SEARCH_TOOL,
                                            arguments={"query": spec["keyword"]})],
                finish_reason="tool_calls",
            )
        spec = spec_by_clause_text(user)
        return LLMResponse(
            content=f"<answer>{json.dumps(spec['answer'], ensure_ascii=False)}</answer>",
            finish_reason="stop",
        )

    def invoke(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        self.calls.append("invoke")
        raise RuntimeError("mock LLM has no sync invoke")
