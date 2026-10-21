"""ReAct agent — a native tool-calling loop over an ``LLM`` and a ``ToolRegistry``.

Each episode is one question:

    system + question → LLM.ainvoke(messages, tools=registry.definitions())
    → response.tool_calls → run the round's calls concurrently under a cap →
    registry.execute(name, args) → tool messages → repeat → a response with no
    tool calls = the final answer

The turn budget caps tool calls per episode; hitting it unbinds the tools and
forces a final answer so an episode can never run away. Every tool call is
recorded on the trajectory.

All prompt text lives in ``agent/prompts`` and is injected — the loop itself
carries no wording, no output contract and no domain assumption. Callers shape
the episode through ``system_prompt``, ``force_answer_msg`` and ``user_prompt``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable

from chillrag.llm import LLM, LLMResponse, ToolCallRequest
from chillrag.tools import ToolRegistry
from chillrag.utils import get_logger
from .prompts import build_user_prompt, get_force_answer_msg, get_system_prompt, prompt_lang

logger = get_logger()


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
    answer: str  # the final assistant message, verbatim
    trajectory: list[AgentTurn]
    num_tool_calls: int
    total_tokens: int
    terminated: str  # "answer" | "max_tool_calls" | "error"
    messages: list[dict[str, Any]] = field(default_factory=list)  # full transcript


@dataclass
class AgentEvent:
    """One event emitted by a streamed episode.

    ``kind``: "tool" (a tool call ran), "delta" (a slice of the answer),
    "done" (finished) or "error".
    """

    kind: str
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    answer: str = ""
    num_tool_calls: int = 0
    terminated: str = ""
    message: str = ""


class ReActAgent:
    """Native tool-calling agent loop over an ``LLM`` facade + ``ToolRegistry``."""

    def __init__(
        self,
        llm: LLM,
        tools: ToolRegistry,
        *,
        max_tool_calls: int | None = None,
        temperature: float = 0.0,  # deterministic sampling — reproducible runs
        max_completion_tokens: int = 4096,
        tool_concurrency: int = 4,
        system_prompt: str | None = None,
        force_answer_msg: str | None = None,
        lang: str = "",
        instructions: str | None = None,
        user_prompt: Callable[[str], str] | None = None,
    ) -> None:
        """``system_prompt`` / ``force_answer_msg`` override the defaults outright.

        Otherwise the default document-QA prompt is used, rendered in ``lang``
        and extended with ``instructions`` (the caller's own requirements).

        ``user_prompt`` maps a question to the user message sent for it — the
        hook a caller uses to attach an output format or per-question context.
        Left unset, the question goes through verbatim.
        """
        self.llm = llm
        self.tools = tools
        self.max_tool_calls = max_tool_calls
        self.tool_concurrency = tool_concurrency
        self.temperature = temperature
        self.max_completion_tokens = max_completion_tokens

        prompt = system_prompt or get_system_prompt(lang, instructions).format(
            tools=tools.tool_hints(prompt_lang(lang))
        )
        self.system_prompt = prompt
        self.force_answer_msg = force_answer_msg or get_force_answer_msg(lang)
        self.user_prompt = user_prompt

    def run(self, question: str) -> AgentResult:
        """Sync convenience wrapper around the async loop."""
        return asyncio.run(self.arun(question))

    async def arun(
        self, question: str, history: list[dict[str, Any]] | None = None
    ) -> AgentResult:
        """Run one episode for ``question``.

        ``history`` is prior user/assistant turns to prepend (multi-turn chat);
        each is ``{"role": "user"|"assistant", "content": str}``.
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for turn in history or []:
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append(
            {
                "role": "user",
                "content": self.user_prompt(question)
                if self.user_prompt
                else build_user_prompt(question),
            }
        )
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
                logger.error("LLM call failed; ending the episode: %s", resp.content)
                break
            messages.append(_assistant_message(resp))

            if tools is None and resp.has_tool_calls:
                # The model ignored the tool-unbinding; drop the stray calls.
                logger.warning("model called tools after they were unbound; ignoring")
                messages[-1].pop("tool_calls", None)
                break
            if not resp.has_tool_calls:
                break

            if self.max_tool_calls is None:
                # No tool budget: let the model run until it answers on its own;
                # the caller's wall-clock ``episode_timeout`` bounds runaway loops.
                executed = resp.tool_calls
            else:
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

            if self.max_tool_calls is not None and (
                num_tool_calls >= self.max_tool_calls or len(executed) < len(resp.tool_calls)
            ):
                # Budget hit (mid-batch or exactly): drop any un-executed calls
                # and force a final answer without tools.
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

    async def arun_stream(
        self, question: str, history: list[dict[str, Any]] | None = None
    ):
        """Run one episode, yielding :class:`AgentEvent` as it progresses.

        Tool calls are emitted as they execute; the answer is emitted as a run
        of "delta" events (so a caller renders it incrementally) then "done".
        ``history`` is prior user/assistant turns to prepend (multi-turn chat).
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for turn in history or []:
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append(
            {
                "role": "user",
                "content": self.user_prompt(question)
                if self.user_prompt
                else build_user_prompt(question),
            }
        )
        num_tool_calls = 0
        terminated = "answer"
        tools = self.tools.definitions()
        answer = ""

        while True:
            content_parts: list[str] = []
            round_tool_calls: list[ToolCallRequest] = []
            finish = "stop"
            async for chunk in self.llm.astream(
                messages,
                tools=tools,
                temperature=self.temperature,
                max_completion_tokens=self.max_completion_tokens,
            ):
                if chunk.content:
                    content_parts.append(chunk.content)
                    # A tool round emits tool-call deltas (no content), so any
                    # content here is answer text — stream it live.
                    yield AgentEvent(kind="delta", text=chunk.content)
                if chunk.tool_calls is not None:
                    round_tool_calls = chunk.tool_calls
                    finish = chunk.finish_reason
            content = "".join(content_parts)

            if finish == "error":
                terminated = "error"
                yield AgentEvent(kind="error", message=content or "LLM call failed")
                break

            resp = LLMResponse(content=content, tool_calls=round_tool_calls, finish_reason=finish)
            messages.append(_assistant_message(resp))

            if tools is None and round_tool_calls:
                messages[-1].pop("tool_calls", None)
                break
            if not round_tool_calls:
                answer = content
                break

            executed = round_tool_calls if self.max_tool_calls is None else round_tool_calls[
                : max(self.max_tool_calls - num_tool_calls, 0)
            ]
            sem = asyncio.Semaphore(self.tool_concurrency)

            async def _run_call(tc: ToolCallRequest) -> AgentTurn:
                async with sem:
                    return await asyncio.to_thread(self._execute_tool, tc, resp)

            turns = await asyncio.gather(*(_run_call(tc) for tc in executed))
            for tc, turn in zip(executed, turns):
                num_tool_calls += 1
                turn.step = num_tool_calls
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": turn.tool_result})
                yield AgentEvent(kind="tool", tool_name=turn.tool_name, tool_args=turn.tool_args)

            if self.max_tool_calls is not None and (
                num_tool_calls >= self.max_tool_calls or len(executed) < len(round_tool_calls)
            ):
                messages[-1]["tool_calls"] = [_tool_call_dict(tc) for tc in executed]
                tools = None
                messages.append({"role": "user", "content": self.force_answer_msg})
                terminated = "max_tool_calls"

        yield AgentEvent(
            kind="done", answer=answer, num_tool_calls=num_tool_calls, terminated=terminated
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
