"""ReAct agent — the clause-comparison agent loop.

Reference: FinIndicator-Bench's ``react_agent.py`` (BrowseComp-Plus's
``run_conversation_with_tools`` OpenAI native tool-calling loop), simplified
onto the OpenAI-compatible ``LLM`` facade and ``ToolRegistry``. Each episode is
one external regulation clause:

    system + question → LLM.invoke(messages, tools=registry.definitions())
    → response.tool_calls → run the round's calls concurrently under a cap →
    registry.execute(name, args) → tool messages → repeat → a response with no
    tool calls = the final answer

The turn budget caps tool calls per episode; hitting it unbinds the tools and
forces a final answer so an episode never runs away. Every tool call is recorded
on the trajectory for post-hoc analysis / grounding. Prompt text lives in
``agent/prompts.py``; ``persist_result`` serializes an episode to disk.

Extension over the reference: ``answer_parser`` — a ``Callable[[str], Any]``
applied to the final answer body, with the result stashed in
``AgentResult.parsed_answer`` (e.g. the coverage JSON for a clause episode).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable

from clause_splitter.utils.async_util import run_coro

from clause_splitter.llm import LLM, LLMResponse, ToolCallRequest
from clause_splitter.tools import ToolRegistry
from clause_splitter.utils import get_logger
from .prompts import build_user_prompt, get_force_answer_msg, get_system_prompt, prompt_lang

logger = get_logger()

#: The ``<answer>`` tag the model must wrap its final value in. For a clause
#: episode the body is a JSON string, e.g. ``<answer>{"status": "完全覆盖", ...}</answer>``.
_ANSWER_RE = re.compile(r"<answer>(.*?)</answer>", re.DOTALL)


def _extract_answer(raw_answer: str) -> tuple[str, str]:
    """Split the final answer into ``(body, unit)``.

    Prefers the ``<answer>…</answer>`` body verbatim; a unit is only extracted
    when the body uses the two-tag numeric form ``<num>…</num><unit>…</unit>``
    (legacy indicator episodes). For clause-coverage episodes the body is the
    JSON string and the unit is empty.
    """
    m = _ANSWER_RE.search(raw_answer)
    if m is None:
        return raw_answer.strip(), ""
    body = m.group(1).strip()
    num = re.search(r"<num>(.*?)</num>", body, re.DOTALL)
    if num is None:
        return body, ""
    unit = re.search(r"<unit>(.*?)</unit>", body, re.DOTALL)
    return num.group(1).strip(), (unit.group(1).strip() if unit else "")


@dataclass
class AgentTurn:
    """One executed tool call within an episode."""

    step: int
    tool_name: str
    tool_args: dict[str, Any]
    tool_result: str
    tool_time_s: float
    reasoning: str | None = None  # assistant text that preceded the call
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class AgentResult:
    """One completed episode."""

    question: str
    answer: str  # the <answer>…</answer> body of the final assistant message
    unit: str  # '' for clause episodes
    raw_answer: str  # the full final assistant message
    parsed_answer: Any = None  # answer_parser(raw_answer), if an answer_parser was set
    trajectory: list[AgentTurn] = field(default_factory=list)
    num_tool_calls: int = 0
    total_tokens: int = 0
    terminated: str = "answer"  # "answer" | "max_tool_calls" | "error"
    messages: list[dict[str, Any]] = field(default_factory=list)  # full transcript


class ReActAgent:
    """Native tool-calling agent loop over an ``LLM`` facade + ``ToolRegistry``."""

    def __init__(
        self,
        llm: LLM,
        tools: ToolRegistry,
        *,
        max_tool_calls: int = 15,
        temperature: float = 0.0,  # deterministic sampling — reproducible runs
        max_completion_tokens: int = 4096,
        tool_concurrency: int = 4,
        system_prompt: str | None = None,
        answer_parser: Callable[[str], Any] | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_tool_calls = max_tool_calls
        self.tool_concurrency = tool_concurrency
        self.temperature = temperature
        self.max_completion_tokens = max_completion_tokens
        self.answer_parser = answer_parser
        # Filled per-agent with the tools actually bound (name + one-line hint,
        # in the prompt's language). The active language comes from
        # ``config.PROMPT_LANG`` unless overridden.
        prompt = system_prompt or get_system_prompt()
        self.system_prompt = prompt.format(tools=tools.tool_hints(prompt_lang()))
        self.force_answer_msg = get_force_answer_msg()

    def run(self, question: str) -> AgentResult:
        """Sync convenience wrapper around the async loop.

        循环感知:已有运行中 loop(Jupyter / IPython / FastAPI)时在独立线程执行,
        避免 "asyncio.run() cannot be called from a running event loop"。
        对比流程内部走 ``arun``(async),此方法仅供外部同步调用方使用。
        """
        return run_coro(self.arun(question))

    async def arun(self, question: str) -> AgentResult:
        """Run one episode for ``question``.

        Awaitable so a batch of independent episodes (one per external clause)
        can run concurrently over the shared ``AsyncOpenAI`` client.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": build_user_prompt(question)},
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
                temperature=self.temperature,
                max_completion_tokens=self.max_completion_tokens,
            )
            total_tokens += resp.usage.get("total_tokens", 0)
            if resp.finish_reason == "error":
                terminated = "error"
                logger.error("LLM invoke failed; ending episode")
                break
            messages.append(_assistant_message(resp))

            if tools is None and resp.has_tool_calls:
                # The model ignored the tool-unbinding; drop the stray calls.
                messages[-1].pop("tool_calls", None)
                break
            if not resp.has_tool_calls:
                break

            executed = resp.tool_calls[: max(self.max_tool_calls - num_tool_calls, 0)]
            # A round can carry several tool calls (e.g. parallel searches); run
            # them concurrently under ``tool_concurrency``, then append results
            # in the original order — tool messages key on tool_call_id, but a
            # stable transcript keeps the trajectory deterministic.
            sem = asyncio.Semaphore(self.tool_concurrency)

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
                # Budget hit (mid-batch or exactly): drop any un-executed calls
                # and force a final answer without tools.
                messages[-1]["tool_calls"] = [_tool_call_dict(tc) for tc in executed]
                tools = None
                messages.append({"role": "user", "content": self.force_answer_msg})
                terminated = "max_tool_calls"

        last = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        raw_answer = last["content"] if last and isinstance(last["content"], str) else ""
        answer, unit = _extract_answer(raw_answer)
        return AgentResult(
            question=question,
            answer=answer,
            unit=unit,
            raw_answer=raw_answer,
            parsed_answer=self.answer_parser(raw_answer) if self.answer_parser else None,
            trajectory=trajectory,
            num_tool_calls=num_tool_calls,
            total_tokens=total_tokens,
            terminated=terminated,
            messages=messages,
        )

    def _execute_tool(self, tc: ToolCallRequest, resp: LLMResponse) -> AgentTurn:
        """Execute one tool call, returning its turn.

        Sync and free of shared-state mutation (the caller assigns the global
        ``step`` and appends the tool message after the round gathers), so a
        round's calls can run concurrently via ``asyncio.to_thread`` race-free.
        """
        t0 = perf_counter()
        try:
            result = self.tools.execute(tc.name, tc.arguments)
        except Exception as e:
            logger.warning("tool %s failed: %s", tc.name, e)
            result = f"Error executing tool {tc.name}: {e}"
        return AgentTurn(
            step=0,  # caller assigns the global step after the round gathers
            tool_name=tc.name,
            tool_args=tc.arguments,
            tool_result=result,
            tool_time_s=round(perf_counter() - t0, 4),
            reasoning=resp.content,
            usage=resp.usage,
        )


# ------------------------------------------------- OpenAI wire format -----


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
