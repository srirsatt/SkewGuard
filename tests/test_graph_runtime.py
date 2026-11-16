"""Bind graph nodes to the intended release environments and sandbox server."""

# LangGraph's optional hooks expose unbound generics in its public method types.
# pyright: reportUnknownMemberType=false

import importlib
from dataclasses import replace
from unittest.mock import Mock

import pytest
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.runtime import Runtime

from skewguard.exec.envs import EnvironmentSetupError
from skewguard.graph.nodes.static_analyze import static_analyze
from skewguard.graph.runtime import RunResources, validate_resources
from skewguard.graph.state import RunState
from skewguard.static.migrations import MigrationInspectionError, compare_inventories
from skewguard.static.types import (
    AnalysisIssue,
    CodeReferenceScan,
    MigrationInventory,
    SQLExtraction,
    StaticContext,
)

node_module = importlib.import_module("skewguard.graph.nodes.static_analyze")


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
                    kind="dynamic_reference", message="Dynamic query unresolved."
                ),
            ],
        ),
    )


def test_runtime_context_reaches_the_node_without_entering_serialized_state(
    graph_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = graph_inputs
    expected = observations()
    collect = Mock(return_value=expected)
    monkeypatch.setattr(node_module, "collect_static_context", collect)

    builder = StateGraph(RunState, context_schema=RunResources)
    builder.add_node("static_analyze", static_analyze)
    builder.add_edge(START, "static_analyze")
    builder.add_edge("static_analyze", END)
    result = builder.compile().invoke(state, context=resources, version="v2")

    assert isinstance(result.value, RunState)
    assert isinstance(result.value.static_context, StaticContext)
    assert result.value.static_context == expected
    assert result.value.static_context.head_code.issues == expected.head_code.issues
    assert result.value.config == state.config
    assert result.value.base_sha == state.base_sha
    assert result.value.head_sha == state.head_sha
    assert state.static_context is None
    saved = result.value.model_dump_json()
    assert resources.server.password.get_secret_value() not in saved
    assert str(resources.base.venv) not in saved
    assert str(resources.head.venv) not in saved
    collect.assert_called_once_with(
        state.config,
        resources.base,
        resources.head,
        resources.server,
        settings_module=state.settings_module,
    )


@pytest.mark.parametrize(
    "mismatch",
    ["base_commit", "head_commit", "base_environment", "head_environment"],
)
def test_mismatched_resources_fail_before_application_inspection(
    graph_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    state, resources = graph_inputs
    if mismatch.endswith("commit"):
        label = mismatch.removesuffix("_commit")
        resources = replace(
            resources,
            checkouts=resources.checkouts.model_copy(
                update={f"{label}_sha": "c" * 40},
            ),
        )
    elif mismatch == "base_environment":
        resources = replace(resources, base=resources.head)
    else:
        resources = replace(resources, head=resources.base)
    collect = Mock()
    monkeypatch.setattr(node_module, "collect_static_context", collect)

    with pytest.raises(ValueError, match="match graph state|belong to its runtime"):
        static_analyze(state, Runtime(context=resources))

    collect.assert_not_called()


@pytest.mark.parametrize("missing", ["interpreter", "venv_config", "backend"])
def test_stale_resources_fail_before_application_inspection(
    graph_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    state, resources = graph_inputs
    if missing == "interpreter":
        resources.base.python.unlink()
    elif missing == "venv_config":
        (resources.base.venv / "pyvenv.cfg").unlink()
    else:
        (resources.base.checkout / state.config.backend_path).rmdir()
    collect = Mock()
    monkeypatch.setattr(node_module, "collect_static_context", collect)

    with pytest.raises(EnvironmentSetupError):
        static_analyze(state, Runtime(context=resources))

    collect.assert_not_called()


def test_backend_cannot_escape_a_runtime_checkout(
    graph_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = graph_inputs
    backend = resources.base.checkout / state.config.backend_path
    backend.rmdir()
    backend.symlink_to(state.config.repo_path, target_is_directory=True)
    collect = Mock()
    monkeypatch.setattr(node_module, "collect_static_context", collect)

    with pytest.raises(EnvironmentSetupError, match="stay inside"):
        static_analyze(state, Runtime(context=resources))

    collect.assert_not_called()


def test_missing_runtime_context_is_an_explicit_error(
    graph_inputs: tuple[RunState, RunResources],
) -> None:
    state, _ = graph_inputs
    with pytest.raises(ValueError, match="must be supplied"):
        validate_resources(state, None)


def test_inspection_failure_propagates_without_fabricating_context(
    graph_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = graph_inputs
    error = MigrationInspectionError("Cannot load the migration graph.")
    monkeypatch.setattr(node_module, "collect_static_context", Mock(side_effect=error))

    with pytest.raises(MigrationInspectionError) as raised:
        static_analyze(state, Runtime(context=resources))

    assert raised.value is error
    assert state.static_context is None
