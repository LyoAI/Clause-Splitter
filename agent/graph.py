"""LangGraph 状态图构建 —— 组装完整流水线.

流水线: parse → analyze_structure → plan_batches → [decompose_simple | decompose_complex] → validate → export

条件路由:
- plan_batches 后：根据复杂度路由到 decompose_simple 或 decompose_complex
- decompose 后：如果还有未处理的 batch，循环回 decompose；否则进入 validate
- validate 后：通过 → export；失败 → self_correct → validate（最多3次）
"""

from __future__ import annotations

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from agent.state import AgentState, create_initial_state
from agent.nodes.parse_document import parse_document_node
from agent.nodes.analyze_structure import analyze_structure_node
from agent.nodes.plan_batches import plan_batches_node
from agent.nodes.decompose_simple import decompose_simple_node
from agent.nodes.decompose_complex import decompose_complex_node
from agent.nodes.validate import validate_node, route_after_validate, self_correct_node
from agent.nodes.export_excel import export_excel_node
from utils import get_logger

logger = get_logger()


# ── 条件路由函数 ──────────────────────────────────────────────────────────

def route_by_complexity(state: dict) -> str:
    """根据当前 batch 中条款的复杂度决定路由.

    如果当前 batch 中有任何一个条款是 complex，则使用 ReAct 模式。
    否则使用 simple 模式。

    Returns:
        "complex" 或 "simple"
    """
    batches = state.get("batches", [])
    batch_index = state.get("current_batch_index", 0)

    if batch_index >= len(batches):
        return "validate"

    current_batch = batches[batch_index]
    for article in current_batch:
        if article.get("complexity_label") == "complex":
            logger.debug("批次 %d → ReAct 复杂拆分", batch_index + 1)
            return "complex"

    logger.debug("批次 %d → One-shot 简单拆分", batch_index + 1)
    return "simple"


def route_after_decompose(state: dict) -> str:
    """拆分后路由：还有未处理的 batch → 继续拆分；否则 → 验证.

    Returns:
        "continue" 或 "validate"
    """
    batches = state.get("batches", [])
    batch_index = state.get("current_batch_index", 0)

    if batch_index < len(batches):
        logger.debug("继续下一批次: %d/%d", batch_index + 1, len(batches))
        return "continue"

    logger.debug("所有批次完成，进入验证")
    return "validate"


# ── Graph 构建 ────────────────────────────────────────────────────────────

def build_split_graph() -> StateGraph:
    """构建条款拆分流水线的 LangGraph 状态图.

    Returns:
        编译后的 StateGraph（带 MemorySaver checkpoint）。
    """
    workflow = StateGraph(AgentState)

    # ── 添加节点 ──
    workflow.add_node("parse_document", parse_document_node)
    workflow.add_node("analyze_structure", analyze_structure_node)
    workflow.add_node("plan_batches", plan_batches_node)
    workflow.add_node("decompose_simple", decompose_simple_node)
    workflow.add_node("decompose_complex", decompose_complex_node)
    workflow.add_node("validate", validate_node)
    workflow.add_node("self_correct", self_correct_node)
    workflow.add_node("export_excel", export_excel_node)

    # ── 入口 ──
    workflow.set_entry_point("parse_document")

    # ── 线性边（确定性流程）──
    workflow.add_edge("parse_document", "analyze_structure")
    workflow.add_edge("analyze_structure", "plan_batches")

    # ── 条件路由: 复杂度判定 ──
    workflow.add_conditional_edges(
        "plan_batches",
        route_by_complexity,
        {
            "simple": "decompose_simple",
            "complex": "decompose_complex",
            "validate": "validate",  # 没有 batches 时直接验证
        },
    )

    # ── 条件路由: 拆分后继续或验证 ──
    workflow.add_conditional_edges(
        "decompose_simple",
        route_after_decompose,
        {
            "continue": "plan_batches",  # 循环回 plan_batches，由 router 决定下一个 batch
            "validate": "validate",
        },
    )
    workflow.add_conditional_edges(
        "decompose_complex",
        route_after_decompose,
        {
            "continue": "plan_batches",
            "validate": "validate",
        },
    )

    # ── 条件路由: 验证后导出或自修正 ──
    workflow.add_conditional_edges(
        "validate",
        route_after_validate,
        {
            "export": "export_excel",
            "self_correct": "self_correct",
            "fail": "export_excel",  # 超过重试次数，强制导出
        },
    )

    # ── Self-correct → 回到验证 ──
    workflow.add_edge("self_correct", "validate")

    # ── 导出 → 结束 ──
    workflow.add_edge("export_excel", END)

    # ── 编译 ──
    checkpointer = MemorySaver()
    graph = workflow.compile(checkpointer=checkpointer)

    logger.info("Agent 状态图构建完成")
    return graph


# ── 便捷方法 ──────────────────────────────────────────────────────────────

async def run_pipeline(
    file_path: str,
    document_name: str,
    llm: object = None,
) -> dict:
    """便捷方法：一键运行完整流水线.

    Args:
        file_path: 上传文件的路径。
        document_name: 原始文件名。
        llm: LLM 实例（可选，注入到 state 中）。

    Returns:
        最终的 state dict。
    """
    graph = build_split_graph()

    initial_state = create_initial_state(
        file_path=file_path,
        document_name=document_name,
        llm=llm,
    )

    config = {"configurable": {"thread_id": f"pipeline-{document_name}"}}
    final_state = await graph.ainvoke(initial_state, config=config)
    return final_state
