"""Build the execution evidence report after graph verification completes."""

from typing import TypedDict

from langgraph.runtime import Runtime

from skewguard.graph.runtime import RunResources, validate_resources
from skewguard.graph.state import RunState
from skewguard.report.json_report import RunReport, build_report


class ReportUpdate(TypedDict):
    """The validated final report for this run."""

    report: RunReport


def report(state: RunState, runtime: Runtime[RunResources]) -> ReportUpdate:
    """Derive the report from execution evidence and the matching pinned releases."""
    if state.verification is None:
        raise ValueError("report requires completed verification evidence")
    resources = validate_resources(state, runtime.context)
    result = build_report(
        state.config,
        resources.checkouts,
        state.verification,
        settings_module=state.settings_module,
        seed_fixture=state.seed_fixture,
    )
    return {"report": result}
