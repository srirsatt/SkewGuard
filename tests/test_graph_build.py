"""Verify the graph's phase ordering and execution-derived report conclusions."""

# LangGraph's optional hooks expose unbound generics in its public method types.
# pyright: reportUnknownMemberType=false

import importlib
from collections.abc import Callable
from unittest.mock import Mock

import pytest
from langgraph.runtime import Runtime

from skewguard.exec.probes import ProbeExecutionError
from skewguard.graph.build import build_graph
from skewguard.graph.nodes.execute_matrix import execute_matrix
from skewguard.graph.nodes.report import report
from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState
from skewguard.report.json_report import RunReport
from skewguard.static.migrations import MigrationInspectionError, compare_inventories
from skewguard.static.types import (
    AnalysisIssue,
    CodeReferenceScan,
    MigrationInventory,
    SQLExtraction,
    StaticContext,
)

analysis_node = importlib.import_module("skewguard.graph.nodes.static_analyze")
execution_node = importlib.import_module("skewguard.graph.nodes.execute_matrix")
report_node = importlib.import_module("skewguard.graph.nodes.report")


def observations() -> StaticContext:
    inventory = MigrationInventory(migrations=[])
    return StaticContext(
        migrations=compare_inventories(inventory, inventory),
        sql=SQLExtraction(results=[]),
        base_code=CodeReferenceScan(references=[], sql_literals=[], issues=[]),
        head_code=CodeReferenceScan(
            references=[],
            sql_literals=[],
            issues=[
                AnalysisIssue(
                    kind="dynamic_reference", message="Dynamic SQL unresolved."
                ),
            ],
        ),
    )


@pytest.mark.parametrize(
    "mode",
    ["safe", "breaking", "flaky", "inconclusive", "coverage", "head_failure"],
)
def test_graph_preserves_phase_order_evidence_and_existing_report_decisions(
    graph_inputs: tuple[RunState, RunResources],
    report_factory: Callable[[str], RunReport],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    state, resources = graph_inputs
    sample = report_factory(mode)
    context = observations()
    inspect = Mock(return_value=context)
    verify = Mock(return_value=sample.verification)
    render = Mock(wraps=report_node.build_report)
    calls = Mock()
    calls.attach_mock(inspect, "static_analyze")
    calls.attach_mock(verify, "execute_matrix")
    calls.attach_mock(render, "report")
    monkeypatch.setattr(analysis_node, "collect_static_context", inspect)
    monkeypatch.setattr(execution_node, "verify_matrix", verify)
    monkeypatch.setattr(report_node, "build_report", render)

    output = build_graph().invoke(state, context=resources, version="v2")

    assert isinstance(output.value, RunState)
    assert output.value.static_context == context
    assert output.value.verification == sample.verification
    assert isinstance(output.value.report, RunReport)
    assert output.value.report.verification == sample.verification
    assert output.value.report.summary == sample.summary
    assert output.value.report.context.base_sha == state.base_sha
    assert output.value.report.context.head_sha == state.head_sha
    assert output.value.report.context.backend_path == state.config.backend_path
    assert [call[0] for call in calls.mock_calls] == [
        "static_analyze",
        "execute_matrix",
        "report",
    ]
    verify.assert_called_once_with(
        state.config,
        resources.base,
        resources.head,
        resources.server,
        settings_module=state.settings_module,
        seed_fixture=state.seed_fixture,
    )
    assert not output.interrupts
    assert state.static_context is None
    assert state.verification is None
    assert state.report is None


@pytest.mark.parametrize("stage", ["analysis", "execution"])
def test_fatal_phase_errors_stop_the_graph_without_a_fabricated_report(
    graph_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    state, resources = graph_inputs
    error = (
        MigrationInspectionError("Inspection failed.")
        if stage == "analysis"
        else ProbeExecutionError("Execution failed.")
    )
    inspect = Mock(
        return_value=observations(), side_effect=error if stage == "analysis" else None
    )
    verify = Mock(side_effect=error if stage == "execution" else None)
    render = Mock()
    monkeypatch.setattr(analysis_node, "collect_static_context", inspect)
    monkeypatch.setattr(execution_node, "verify_matrix", verify)
    monkeypatch.setattr(report_node, "build_report", render)

    with pytest.raises(type(error)) as raised:
        build_graph().invoke(state, context=resources, version="v2")

    assert raised.value is error
    inspect.assert_called_once()
    if stage == "analysis":
        verify.assert_not_called()
    else:
        verify.assert_called_once()
    render.assert_not_called()
    assert state.report is None


def test_execution_requires_the_static_context_phase_first(
    graph_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = graph_inputs
    verify = Mock()
    monkeypatch.setattr(execution_node, "verify_matrix", verify)

    with pytest.raises(ValueError, match="requires completed static-analysis"):
        execute_matrix(state, Runtime(context=resources))

    verify.assert_not_called()


def test_report_requires_execution_evidence(
    graph_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = graph_inputs
    render = Mock()
    monkeypatch.setattr(report_node, "build_report", render)

    with pytest.raises(ValueError, match="requires completed verification"):
        report(state, Runtime(context=resources))

    render.assert_not_called()
