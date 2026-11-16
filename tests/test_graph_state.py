"""Exercise typed graph updates and portable nested verification evidence."""

# LangGraph's optional hooks expose unbound generics in its public method types.
# pyright: reportUnknownMemberType=false

from collections.abc import Callable
from typing import cast

import pytest
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from pydantic import ValidationError

from skewguard.config import RunConfig
from skewguard.exec.types import VerificationResult
from skewguard.graph.state import RunState
from skewguard.report.json_report import RunReport
from skewguard.static.migrations import compare_inventories
from skewguard.static.types import (
    CodeReferenceScan,
    MigrationInventory,
    SQLExtraction,
    StaticContext,
)


def initial_state(report: RunReport) -> RunState:
    context = report.context
    return RunState(
        config=RunConfig(
            repo_path=context.repo_path,
            backend_path=context.backend_path,
            base_ref=context.base_ref,
            head_ref=context.head_ref,
            timeout_seconds=context.timeout_seconds,
            probe_row_limit=context.probe_row_limit,
        ),
        settings_module=context.settings_module,
        base_sha=context.base_sha,
        head_sha=context.head_sha,
        seed_fixture=context.seed_fixture,
    )


def static_context() -> StaticContext:
    inventory = MigrationInventory(migrations=[])
    return StaticContext(
        migrations=compare_inventories(inventory, inventory),
        sql=SQLExtraction(results=[]),
        base_code=CodeReferenceScan(references=[], sql_literals=[], issues=[]),
        head_code=CodeReferenceScan(references=[], sql_literals=[], issues=[]),
    )


def test_partial_graph_updates_preserve_inputs_and_typed_phase_results(
    report_factory: Callable[[str], RunReport],
) -> None:
    report = report_factory("safe")
    initial = initial_state(report)
    observations = static_context()
    visited: list[str] = []

    def analyze(state: RunState) -> dict[str, StaticContext]:
        assert state == initial
        visited.append("static_analyze")
        return {"static_context": observations}

    def execute(state: RunState) -> dict[str, VerificationResult]:
        assert state.config == initial.config
        assert state.base_sha == initial.base_sha
        assert state.head_sha == initial.head_sha
        assert isinstance(state.static_context, StaticContext)
        assert state.static_context == observations
        assert state.verification is None
        visited.append("execute_matrix")
        return {"verification": report.verification}

    def render(state: RunState) -> dict[str, RunReport]:
        assert state.static_context == observations
        assert isinstance(state.verification, VerificationResult)
        assert state.verification == report.verification
        assert state.report is None
        visited.append("report")
        return {"report": report}

    builder = StateGraph(RunState)
    builder.add_node("static_analyze", analyze)
    builder.add_node("execute_matrix", execute)
    builder.add_node("report", render)
    builder.add_edge(START, "static_analyze")
    builder.add_edge("static_analyze", "execute_matrix")
    builder.add_edge("execute_matrix", "report")
    builder.add_edge("report", END)

    result = builder.compile().invoke(initial, version="v2")

    assert isinstance(result.value, RunState)
    assert result.value.config == initial.config
    assert result.value.static_context == observations
    assert result.value.verification == report.verification
    assert result.value.report == report
    assert not result.interrupts
    assert visited == ["static_analyze", "execute_matrix", "report"]
    assert initial.static_context is None
    assert initial.verification is None
    assert initial.report is None


def test_populated_state_preserves_execution_and_analysis_evidence_through_json(
    report_factory: Callable[[str], RunReport],
) -> None:
    report = report_factory("breaking")
    initial = initial_state(report)
    populated = RunState.model_validate(
        {
            **initial.model_dump(),
            "static_context": static_context(),
            "verification": report.verification,
            "report": report,
        }
    )

    restored = RunState.model_validate_json(populated.model_dump_json())

    assert restored == populated
    assert isinstance(restored.config, RunConfig)
    assert isinstance(restored.static_context, StaticContext)
    assert isinstance(restored.verification, VerificationResult)
    assert isinstance(restored.report, RunReport)
    assert restored.report.summary.confirmed > 0


@pytest.mark.parametrize(
    "changes",
    [
        {"base_sha": "main"},
        {"head_sha": "HEAD"},
        {"settings_module": "shop..settings"},
        {"settings_module": "shop/settings"},
        {"seed_fixture": "   "},
    ],
)
def test_invalid_run_inputs_fail_before_a_graph_node_executes(
    report_factory: Callable[[str], RunReport],
    changes: dict[str, str],
) -> None:
    initial = initial_state(report_factory("safe"))
    visited: list[str] = []

    def execute(state: RunState) -> dict[str, VerificationResult | None]:
        visited.append("execute")
        return {"verification": state.verification}

    builder = StateGraph(RunState)
    builder.add_node("execute", execute)
    builder.add_edge(START, "execute")
    builder.add_edge("execute", END)
    graph = builder.compile()

    with pytest.raises(ValidationError):
        # Deliberately pass malformed input to exercise node-entry validation.
        invalid = cast(RunState, {**initial.model_dump(), **changes})
        graph.invoke(invalid, version="v2")

    assert visited == []
