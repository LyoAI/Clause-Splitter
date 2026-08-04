"""ReAct Compare Agent — LLM + BM25 tools for clause matching.

The agent receives a batch of external obligations and has access to
BM25 search tools. It uses a ReAct loop:
1. Extract key elements from external obligations
2. Search the internal obligation index (BM25)
3. Review candidates
4. Refine search if needed
5. Output MatchResult for each external obligation

Parallel batches: each batch runs independently via asyncio.gather.
"""

from __future__ import annotations

import json
import re
from typing import Any

from config.settings import get_settings
from tools.search.bm25_searcher import BM25Searcher
from prompts.compare import SYSTEM_PROMPT, format_agent_user_prompt
from utils import get_logger

logger = get_logger()


# ═══════════════════════════════════════════════════════════════════════════
# Tool implementations (called by LLM via function-calling)
# ═══════════════════════════════════════════════════════════════════════════


def _bm25_search_tool(
    searcher: BM25Searcher,
    query: str,
    top_k: int = 20,
) -> str:
    """BM25 search tool — called by LLM.

    Args:
        searcher: BM25Searcher instance.
        query: Search query string.
        top_k: Number of results.

    Returns:
        JSON string with search results.
    """
    results = searcher.search(query, k=top_k)
    if not results:
        return json.dumps(
            {"count": 0, "message": "未找到相关内规条款", "results": []},
            ensure_ascii=False,
        )

    formatted = []
    for r in results:
        obl = r.get("obligation", {})
        formatted.append({
            "obligation_id": r["docid"],
            "score": round(r["score"], 4),
            "document_name": obl.get("document_name", ""),
            "clause_number": obl.get("clause_number", ""),
            "subject": obl.get("subject", "/"),
            "action_verb": obl.get("action_verb", "/"),
            "action_target": obl.get("action_target", "/"),
            "source_text": (obl.get("source_text", "") or "")[:200],
        })

    return json.dumps(
        {"count": len(formatted), "results": formatted},
        ensure_ascii=False,
        indent=2,
    )


def _get_obligation_detail_tool(
    searcher: BM25Searcher,
    obligation_id: str,
) -> str:
    """Get obligation detail tool — called by LLM.

    Args:
        searcher: BM25Searcher instance.
        obligation_id: Obligation index number.

    Returns:
        JSON string with full obligation details.
    """
    doc = searcher.get_by_id(obligation_id)
    if doc is None:
        # Try extracting from search
        results = searcher.search(f"id:{obligation_id}", k=1)
        if results:
            doc = results[0].get("obligation", {})
        else:
            return json.dumps(
                {"error": f"未找到 ID 为 {obligation_id} 的内规义务"},
                ensure_ascii=False,
            )

    return json.dumps(doc, ensure_ascii=False, indent=2)


def _search_by_fields_tool(
    searcher: BM25Searcher,
    subject: str = "",
    action_verb: str = "",
    action_target: str = "",
    top_k: int = 20,
) -> str:
    """Search by structured fields — called by LLM.

    Constructs a weighted query from individual obligation fields.

    Args:
        searcher: BM25Searcher instance.
        subject: Obligation subject keyword.
        action_verb: Obligation action verb keyword.
        action_target: Obligation action target keyword.
        top_k: Number of results.

    Returns:
        JSON string with search results.
    """
    query_parts = []
    if subject:
        query_parts.append(subject)
    if action_verb:
        query_parts.append(action_verb)
    if action_target:
        query_parts.append(action_target)

    if not query_parts:
        return json.dumps(
            {"count": 0, "message": "请至少指定一个搜索字段", "results": []},
            ensure_ascii=False,
        )

    query = " ".join(query_parts)
    return _bm25_search_tool(searcher, query, top_k)


# ═══════════════════════════════════════════════════════════════════════════
# OpenAI tool definitions (function-calling format)
# ═══════════════════════════════════════════════════════════════════════════


def _get_compare_tools() -> list[dict[str, Any]]:
    """Return OpenAI function-calling tool definitions for the compare agent."""
    return [
        {
            "type": "function",
            "function": {
                "name": "bm25_search",
                "description": (
                    "从内规条款库中 BM25 检索与外规义务最相关的候选条款。"
                    "搜索查询应包含核心关键词（义务主体、动作、事项）。"
                    "返回 top-K 条结果，每条含义务ID、BM25分数、摘要信息。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索查询，使用中文关键词，如'数据安全管理制度 建立'",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "返回结果数，默认20，最大50",
                            "default": 20,
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_obligation_detail",
                "description": (
                    "获取指定内规义务的完整13字段信息。"
                    "用于确认候选条款是否确实匹配外规要求。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "obligation_id": {
                            "type": "string",
                            "description": "内规义务的序号ID",
                        },
                    },
                    "required": ["obligation_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_by_fields",
                "description": (
                    "按义务的结构化字段（主体、动作、事项）分别搜索。"
                    "比自由文本查询更精确，用于当 bm25_search 结果不理想时的精确检索。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": "义务主体关键词，如'数据处理者'",
                        },
                        "action_verb": {
                            "type": "string",
                            "description": "义务动作关键词，如'建立'、'禁止'",
                        },
                        "action_target": {
                            "type": "string",
                            "description": "义务事项关键词，如'数据安全管理制度'",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "返回结果数，默认20",
                            "default": 20,
                        },
                    },
                },
            },
        },
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Tool execution dispatcher
# ═══════════════════════════════════════════════════════════════════════════


def _execute_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    searcher: BM25Searcher,
) -> str:
    """Execute a tool call and return the result string.

    Args:
        tool_name: Name of the tool to execute.
        tool_args: Arguments from the LLM.
        searcher: BM25Searcher instance.

    Returns:
        Tool result as a string (JSON).
    """
    if tool_name == "bm25_search":
        return _bm25_search_tool(
            searcher,
            query=str(tool_args.get("query", "")),
            top_k=int(tool_args.get("top_k", 20)),
        )
    elif tool_name == "get_obligation_detail":
        return _get_obligation_detail_tool(
            searcher,
            obligation_id=str(tool_args.get("obligation_id", "")),
        )
    elif tool_name == "search_by_fields":
        return _search_by_fields_tool(
            searcher,
            subject=str(tool_args.get("subject", "")),
            action_verb=str(tool_args.get("action_verb", "")),
            action_target=str(tool_args.get("action_target", "")),
            top_k=int(tool_args.get("top_k", 20)),
        )
    else:
        return json.dumps(
            {"error": f"未知工具: {tool_name}"},
            ensure_ascii=False,
        )


# ═══════════════════════════════════════════════════════════════════════════
# ReAct Agent Loop
# ═══════════════════════════════════════════════════════════════════════════


async def run_compare_batch(
    external_batch: list[dict[str, Any]],
    searcher: BM25Searcher,
    llm: Any,
    batch_index: int = 0,
    total_batches: int = 1,
) -> list[dict[str, Any]]:
    """Run the ReAct compare agent for one batch of external obligations.

    The agent uses LLM + BM25 tools to find matching internal obligations
    and produce MatchResults.

    Args:
        external_batch: List of external obligation dicts (15 max).
        searcher: BM25Searcher for the internal obligation index.
        llm: LLM instance (ChatOpenAI or compatible, must support tool calling).
        batch_index: Current batch number.
        total_batches: Total number of batches.

    Returns:
        List of MatchResult dicts.
    """
    settings = get_settings()
    max_iterations = settings.compare_max_react_iterations

    system_prompt = SYSTEM_PROMPT
    user_prompt = format_agent_user_prompt(
        external_batch, batch_index, total_batches,
    )

    tools = _get_compare_tools()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    for iteration in range(max_iterations):
        try:
            # Call LLM with tools
            if hasattr(llm, "ainvoke"):
                response = await llm.ainvoke(messages, tools=tools)
            else:
                response = llm.invoke(messages, tools=tools)

            # Extract content and tool calls
            content = ""
            tool_calls = []

            if hasattr(response, "content"):
                content = response.content or ""
            if hasattr(response, "tool_calls"):
                tool_calls = response.tool_calls or []
            elif hasattr(response, "additional_kwargs"):
                tool_calls = (
                    response.additional_kwargs.get("tool_calls", []) or []
                )

            # If no tool calls, the agent is done → parse final output
            if not tool_calls:
                logger.info(
                    "Compare batch %d/%d: LLM finished after %d iterations",
                    batch_index + 1, total_batches, iteration + 1,
                )
                return _parse_final_matches(
                    content, external_batch, messages,
                )

            # Execute tool calls and append results
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            # Format tool calls for the message
            tc_list = []
            for tc in tool_calls:
                tc_dict = {
                    "id": getattr(tc, "id", "call_1"),
                    "type": "function",
                    "function": {
                        "name": getattr(tc, "name", ""),
                        "arguments": (
                            getattr(tc, "args", {})
                            if hasattr(tc, "args")
                            else json.dumps(
                                getattr(tc, "arguments", {}),
                                ensure_ascii=False,
                            )
                        ),
                    },
                }
                tc_list.append(tc_dict)
            assistant_msg["tool_calls"] = tc_list
            messages.append(assistant_msg)

            for tc in tool_calls:
                tool_name = getattr(tc, "name", "")
                tool_args = (
                    getattr(tc, "args", None)
                    if hasattr(tc, "args")
                    else getattr(tc, "arguments", {})
                )
                if isinstance(tool_args, str):
                    try:
                        tool_args = json.loads(tool_args)
                    except json.JSONDecodeError:
                        tool_args = {}

                result = _execute_tool(tool_name, tool_args, searcher)
                messages.append({
                    "role": "tool",
                    "tool_call_id": getattr(tc, "id", "call_1"),
                    "content": result,
                })

        except Exception as e:
            logger.error(
                "Compare batch %d iter %d error: %s",
                batch_index + 1, iteration + 1, e,
            )
            if iteration == max_iterations - 1:
                return _fallback_empty_matches(external_batch)

    # Max iterations reached — try to parse whatever we have
    logger.warning(
        "Compare batch %d: max iterations (%d) reached",
        batch_index + 1, max_iterations,
    )
    return _parse_final_matches(
        messages[-1].get("content", ""), external_batch, messages,
    )


def _parse_final_matches(
    content: str,
    external_batch: list[dict[str, Any]],
    _messages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Parse the final LLM output into MatchResult dicts."""
    if not content:
        return _fallback_empty_matches(external_batch)

    # Try to parse JSON from content
    parsed = _extract_json_array(content)
    if parsed:
        matches = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            matches.append({
                "external_clause_id": str(item.get("external_clause_id", "")),
                "status": item.get("status", "缺失"),
                "matched_internal_id": str(item.get("matched_internal_id", ""))
                if item.get("matched_internal_id") else None,
                "analysis": item.get("analysis", ""),
                "remedy_suggestion": item.get("remedy_suggestion", ""),
            })
        return matches

    # Fallback: mark all as not covered
    return _fallback_empty_matches(external_batch)


def _fallback_empty_matches(
    external_batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return empty match results when LLM fails to produce output."""
    return [
        {
            "external_clause_id": str(obl.get("index", i)),
            "status": "缺失",
            "matched_internal_id": None,
            "analysis": "LLM 未能完成匹配分析",
            "remedy_suggestion": "请人工审核此项",
        }
        for i, obl in enumerate(external_batch)
    ]


def _extract_json_array(content: str) -> list | None:
    """Extract JSON array from LLM output."""
    if not content:
        return None

    # Try code block
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", content)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Try raw parse
    try:
        parsed = json.loads(content.strip())
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Try first [...] block
    m = re.search(r"\[[\s\S]+\]", content)
    if m:
        try:
            parsed = json.loads(m.group())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    return None
