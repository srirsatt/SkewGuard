"""Run execution-based migration verification as a graph step."""

from typing import TypedDict

from langgraph.runtime import Runtime

from skewguard.exec.types import VerificationResult
from skewguard.exec.verify import verify_matrix
from skewguard.graph.runtime import RunResources, validate_resources
from skewguard.graph.state import RunState


class ExecuteMatrixUpdate(TypedDict):
    """Execution evidence, comparisons, and reproduction decisions."""

    verification: VerificationResult


def execute_matrix(
    state: RunState, runtime: Runtime[RunResources]
) -> ExecuteMatrixUpdate:
    """Run the four cells and required fresh-state replays through existing helpers."""
    if state.static_context is None:
        raise ValueError("execute_matrix requires completed static-analysis context")
    resources = validate_resources(state, runtime.context)
    result = verify_matrix(
        state.config,
        resources.base,
        resources.head,
        resources.server,
        settings_module=state.settings_module,
        seed_fixture=state.seed_fixture,
    )
    return {"verification": result}
