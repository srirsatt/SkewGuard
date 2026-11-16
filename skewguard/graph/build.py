"""Assemble the initial deterministic verification workflow."""

# LangGraph's optional hooks expose unbound generics in its public method types.
# pyright: reportUnknownMemberType=false

from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import (  # pyright: ignore[reportMissingTypeStubs]
    CompiledStateGraph,
)

from skewguard.graph.nodes.execute_matrix import execute_matrix
from skewguard.graph.nodes.report import report
from skewguard.graph.nodes.static_analyze import static_analyze
from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState


def build_graph() -> CompiledStateGraph[RunState, RunResources, RunState, RunState]:
    """Connect static context, execution verification, and report construction."""
    builder = StateGraph(RunState, context_schema=RunResources)
    builder.add_node("static_analyze", static_analyze) # basic AST analysis (non LLM)
    builder.add_node("execute_matrix", execute_matrix) # 4 square matrix execution -> 4 runtimes
    builder.add_node("report", report)

    builder.add_edge(START, "static_analyze")
    builder.add_edge("static_analyze", "execute_matrix")
    builder.add_edge("execute_matrix", "report")
    builder.add_edge("report", END)

    return builder.compile()
