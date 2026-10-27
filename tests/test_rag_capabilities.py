"""Provider capability gating — hidden tools, parse skip, fatal auth errors."""

from __future__ import annotations

import pytest

from clause_splitter.agent import ReActAgent, ToolRegistry
from clause_splitter.agent.hints import SEARCH_TOOL
from clause_splitter.llm.provider.base import LLMResponse, ToolCallRequest
from clause_splitter.rag import RagAuthError, RagUnavailable
from tests.fakes import FakeRagService


def _capped_service() -> FakeRagService:
    """A provider like Zeus: search only, no parse/listing/chunk endpoints."""
    service = FakeRagService()
    service.supports_parse = False
    service.supports_document_listing = False
    service.supports_document_chunks = False
    return service


def _agent(service) -> ReActAgent:
    return ReActAgent(
        None, ToolRegistry(service),
        system_prompt="", force_answer_msg="", max_tool_calls=1,
    )


def _search_call() -> ToolCallRequest:
    return ToolCallRequest(id="call-1", name=SEARCH_TOOL, arguments={"query": "制度"})


def test_tool_names_follow_provider_capabilities():
    full = ToolRegistry(FakeRagService())
    assert full.tool_names() == [SEARCH_TOOL, "get_context", "get_materials", "calculate"]

    capped = ToolRegistry(_capped_service())
    assert capped.tool_names() == [SEARCH_TOOL, "calculate"]
    assert {d["function"]["name"] for d in capped.definitions()} == {
        SEARCH_TOOL, "calculate"
    }
    hints = capped.tool_hints()
    assert "get_context" not in hints
    assert "get_materials" not in hints


def test_execute_rejects_hidden_tools():
    registry = ToolRegistry(_capped_service())
    with pytest.raises(KeyError):
        registry.execute("get_context", {"chunk_id": "x", "scope": [0, 0]})
    with pytest.raises(KeyError):
        registry.execute("get_materials", {})


def test_duck_typed_service_without_flags_keeps_all_tools():
    # FakeRagService does not inherit RagService, so it has no capability attrs;
    # getattr(..., True) must keep every tool available.
    service = FakeRagService()
    registry = ToolRegistry(service)
    assert registry.tool_names() == [SEARCH_TOOL, "get_context", "get_materials", "calculate"]
    assert registry.execute("calculate", {"expression": "1+1"})


def test_auth_error_fails_the_episode_instead_of_becoming_a_tool_message():
    class _AuthFail(FakeRagService):
        def search(self, query: str, k: int):
            raise RagAuthError("bad credentials", status_code=401)

    with pytest.raises(RagAuthError):
        _agent(_AuthFail())._execute_tool(_search_call(), LLMResponse(content=None))


def test_transient_error_still_degrades_to_a_tool_message():
    class _Down(FakeRagService):
        def search(self, query: str, k: int):
            raise RagUnavailable("service down")

    turn = _agent(_Down())._execute_tool(_search_call(), LLMResponse(content=None))
    assert turn.tool_result.startswith("Error executing tool")
