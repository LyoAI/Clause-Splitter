"""工具注册中心 —— 统一管理所有 Agent 工具的 name / description / schema.

设计原则：
- 工具定义（OpenAI function schema）与工具实现（Python 函数）分离
- 每个工具在此注册后，可通过 get_openai_tools() 获取 LLM 可用的工具列表
- 通过 execute() 按名称调用具体工具实现
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from utils import get_logger

logger = get_logger()


@dataclass
class ToolDef:
    """单个工具的完整定义.

    Attributes:
        name: 工具名称（LLM 可见）。
        description: 工具描述（LLM 可见，影响 LLM 何时调用此工具）。
        parameters: JSON Schema 格式的参数定义。
        func: Python 实现函数。
        require_llm: 是否需要传入 LLM 实例（如 decompose_article_batch 内部需要调用 LLM）。
        node_only: 是否仅供 graph node 直接调用、不暴露给 LLM（如纯确定性的工具）。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    require_llm: bool = False
    node_only: bool = False


class ToolRegistry:
    """工具注册中心.

    用法：
        registry = ToolRegistry()
        registry.register(ToolDef(name="foo", ...))
        openai_tools = registry.get_openai_tools()
        result = registry.execute("foo", {"arg": "value"})
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    # ── 注册 ──────────────────────────────────────────────────────────

    def register(self, tool: ToolDef) -> None:
        """注册一个工具."""
        if tool.name in self._tools:
            logger.warning("工具 '%s' 已注册，将被覆盖", tool.name)
        self._tools[tool.name] = tool
        logger.debug("工具已注册: %s", tool.name)

    def register_many(self, tools: list[ToolDef]) -> None:
        """批量注册工具."""
        for tool in tools:
            self.register(tool)

    # ── 查询 ──────────────────────────────────────────────────────────

    def get(self, name: str) -> ToolDef | None:
        """按名称获取工具定义."""
        return self._tools.get(name)

    def get_openai_tools(self, include_node_only: bool = False) -> list[dict[str, Any]]:
        """返回 OpenAI function-calling 格式的工具列表.

        Args:
            include_node_only: 是否包含 node_only 工具（默认不包含，仅暴露给 LLM 的工具）。
        """
        result: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if tool.node_only and not include_node_only:
                continue
            result.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            })
        return result

    def get_tool_names(self) -> list[str]:
        """获取所有已注册工具的名称列表."""
        return list(self._tools.keys())

    # ── 执行 ──────────────────────────────────────────────────────────

    def execute(self, name: str, args: dict[str, Any], **extra: Any) -> Any:
        """按名称执行指定工具.

        Args:
            name: 工具名称。
            args: 工具参数（LLM 传入的 arguments）。
            **extra: 额外参数（如 llm 实例、state 等，注入到工具函数中）。

        Returns:
            工具执行结果。

        Raises:
            KeyError: 工具未注册。
        """
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"工具 '{name}' 未注册。已注册工具: {self.get_tool_names()}")

        # 如果工具需要 LLM 实例且 extra 中有，注入
        if tool.require_llm and "llm" in extra:
            return tool.func(**args, llm=extra["llm"])

        return tool.func(**args)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
