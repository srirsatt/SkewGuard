"""Assemble the initial deterministic verification workflow."""

# LangGraph's optional hooks expose unbound generics in its public method types.
# pyright: reportUnknownMemberType=false

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import (  # pyright: ignore[reportMissingTypeStubs]
    CompiledStateGraph,
)

from skewguard.graph.nodes.execute_matrix import execute_matrix
from skewguard.graph.nodes.report import report
from skewguard.graph.nodes.scenario import (
    after_analysis,
    after_qualification,
    propose_scenario,
    qualify_proposal,
)
from skewguard.graph.nodes.static_analyze import static_analyze
from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState

type VerificationGraph = CompiledStateGraph[RunState, RunResources, RunState, RunState]


def build_graph(checkpointer: BaseCheckpointSaver[str] | None = None) -> VerificationGraph:
    """Connect static context, execution verification, and report construction."""
    builder = StateGraph(RunState, context_schema=RunResources)
    builder.add_node("static_analyze", static_analyze) # basic AST analysis (non LLM)
    builder.add_node("execute_matrix", execute_matrix) # 4 square matrix execution -> 4 runtimes
    builder.add_node("report", report)

    builder.add_edge(START, "static_analyze")
    builder.add_node("propose_scenario", propose_scenario)
    builder.add_node("qualify_proposal", qualify_proposal)
    builder.add_conditional_edges("static_analyze", after_analysis)
    builder.add_edge("propose_scenario", "qualify_proposal")
    builder.add_conditional_edges("qualify_proposal", after_qualification)
    builder.add_edge("execute_matrix", "report")
    builder.add_edge("report", END)

    return builder.compile(checkpointer=checkpointer)
