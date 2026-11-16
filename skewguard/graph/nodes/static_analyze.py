"""Collect migration and source context through a graph node."""

from typing import TypedDict

from langgraph.runtime import Runtime

from skewguard.graph.runtime import RunResources, validate_resources
from skewguard.graph.state import RunState
from skewguard.static.context import collect_static_context
from skewguard.static.types import StaticContext


class StaticAnalyzeUpdate(TypedDict):
    """Update only the planning observations produced by this node."""

    static_context: StaticContext


def static_analyze(
    state: RunState, runtime: Runtime[RunResources]
) -> StaticAnalyzeUpdate:
    """Use run-scoped resources and return validated planning context."""
    resources = validate_resources(state, runtime.context)
    result = collect_static_context(
        state.config,
        resources.base,
        resources.head,
        resources.server,
        settings_module=state.settings_module,
    )
    return {"static_context": result}
