"""ReAct loop — native tool calling over an ``LLM`` and a ``ToolRegistry``.

Each episode is one question: system prompt + question → LLM with tool
definitions → execute the round's tool calls concurrently → tool messages →
repeat until the model answers without tool calls. The tool budget caps the
episode; hitting it unbinds tools and forces a final answer.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from clause_splitter.llm.provider.base import LLMResponse, ToolCallRequest
from clause_splitter.rag import RagAuthError, RagNotConfigured
from clause_splitter.utils import get_logger

from .registry import ToolRegistry

logger = get_logger()

_MAX_TOKENS = 4096
_TOOL_CONCURRENCY = 4


@dataclass
class AgentTurn:
    """One executed tool call within an episode."""

    step: int
    tool_name: str
    tool_args: dict[str, Any]
    tool_result: str
    tool_time_s: float
    reasoning: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class AgentResult:
    """One completed episode."""

    question: str
    answer: str
    trajectory: list[AgentTurn]
    num_tool_calls: int
    total_tokens: int
    terminated: str  # "answer" | "max_tool_calls" | "error"
    messages: list[dict[str, Any]] = field(default_factory=list)


class ReActAgent:
    """Tool-calling agent loop over an ``LLM`` facade + ``ToolRegistry``."""

    def __init__(
        self,
        llm: Any,
        tools: ToolRegistry,
        *,
        system_prompt: str,
        force_answer_msg: str,
        max_tool_calls: int,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        self.force_answer_msg = force_answer_msg
        self.max_tool_calls = max_tool_calls

    async def arun(self, question: str) -> AgentResult:
        """Run one episode for ``question``."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question},
        ]
        trajectory: list[AgentTurn] = []
        total_tokens = 0
        num_tool_calls = 0
        terminated = "answer"
        tools = self.tools.definitions()

        while True:
            resp = await self.llm.ainvoke(
                messages,
                tools=tools,
                temperature=0.0,
                max_completion_tokens=_MAX_TOKENS,
            )
            total_tokens += resp.usage.get("total_tokens", 0)
            if resp.finish_reason == "error":
                terminated = "error"
                logger.error("LLM call failed; ending the episode: %s", resp.content)
                break
            messages.append(_assistant_message(resp))

            if tools is None and resp.has_tool_calls:
                logger.warning("model called tools after they were unbound; ignoring")
                messages[-1].pop("tool_calls", None)
                break
            if not resp.has_tool_calls:
                break

            executed = resp.tool_calls[: max(self.max_tool_calls - num_tool_calls, 0)]
            sem = asyncio.Semaphore(_TOOL_CONCURRENCY)

            async def _run_call(tc: ToolCallRequest) -> AgentTurn:
                async with sem:
                    return await asyncio.to_thread(self._execute_tool, tc, resp)

            turns = await asyncio.gather(*(_run_call(tc) for tc in executed))
            for tc, turn in zip(executed, turns):
                num_tool_calls += 1
                turn.step = num_tool_calls
                trajectory.append(turn)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": turn.tool_result})

            if num_tool_calls >= self.max_tool_calls or len(executed) < len(resp.tool_calls):
                messages[-1]["tool_calls"] = [_tool_call_dict(tc) for tc in executed]
                tools = None
                messages.append({"role": "user", "content": self.force_answer_msg})
                terminated = "max_tool_calls"
                logger.info(
                    "tool budget of %d reached; forcing a final answer", self.max_tool_calls
                )

        last = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        answer = last["content"] if last and isinstance(last["content"], str) else ""
        return AgentResult(
            question=question,
            answer=answer,
            trajectory=trajectory,
            num_tool_calls=num_tool_calls,
            total_tokens=total_tokens,
            terminated=terminated,
            messages=messages,
        )

    def _execute_tool(self, tc: ToolCallRequest, resp: LLMResponse) -> AgentTurn:
        """Execute one tool call (sync; safe to run in worker threads)."""
        t0 = perf_counter()
        try:
            result = self.tools.execute(tc.name, tc.arguments)
        except (RagAuthError, RagNotConfigured):
            # Credential/configuration errors must fail the job, not become a
            # tool message that silently degrades every clause to "缺失".
            raise
        except Exception as e:  # noqa: BLE001 — tool failure becomes a tool message
            logger.warning("tool %s failed: %s", tc.name, e)
            result = f"Error executing tool {tc.name}: {e}"
        return AgentTurn(
            step=0,
            tool_name=tc.name,
            tool_args=tc.arguments,
            tool_result=result,
            tool_time_s=round(perf_counter() - t0, 4),
            reasoning=resp.content,
            usage=resp.usage,
        )


def _assistant_message(resp: LLMResponse) -> dict[str, Any]:
    """An assistant turn, echoing native tool calls (OpenAI wire format)."""
    msg: dict[str, Any] = {"role": "assistant", "content": resp.content}
    if resp.has_tool_calls:
        msg["tool_calls"] = [_tool_call_dict(tc) for tc in resp.tool_calls]
    return msg


def _tool_call_dict(tc: ToolCallRequest) -> dict[str, Any]:
    return {
        "id": tc.id,
        "type": "function",
        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
    }
