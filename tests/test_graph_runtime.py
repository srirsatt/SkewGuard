"""Bind graph nodes to the intended release environments and sandbox server."""

# LangGraph's optional hooks expose unbound generics in its public method types.
# pyright: reportUnknownMemberType=false

import importlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.runtime import Runtime
from pydantic import SecretStr

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import (
    EnvironmentSetupError,
    PythonEnvironment,
    environment_python,
)
from skewguard.exec.worktree import CheckoutPair
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


@pytest.fixture
def run_inputs(tmp_path: Path) -> tuple[RunState, RunResources]:
    environments: list[PythonEnvironment] = []
    for label in ("base", "head"):
        checkout = tmp_path / label
        (checkout / "backend").mkdir(parents=True)
        venv = checkout / ".skewguard-venv"
        python = environment_python(venv)
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        (venv / "pyvenv.cfg").write_text("", encoding="utf-8")
        environments.append(
            PythonEnvironment(
                checkout=checkout,
                venv=venv,
                python=python,
            )
        )
    config = RunConfig(
        repo_path=tmp_path,
        base_ref="release-v1",
        head_ref="release-v2",
        backend_path=Path("backend"),
    )
    checkouts = CheckoutPair(
        base=environments[0].checkout,
        head=environments[1].checkout,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    return (
        RunState(
            config=config,
            settings_module="shop.settings",
            base_sha=checkouts.base_sha,
            head_sha=checkouts.head_sha,
        ),
        RunResources(
            checkouts=checkouts,
            base=environments[0],
            head=environments[1],
            server=DatabaseConnection(
                host="localhost",
                port=5432,
                username="test",
                dbname="postgres",
                password=SecretStr("runtime-only-database-credential"),
            ),
        ),
    )


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
    run_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = run_inputs
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
    run_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    state, resources = run_inputs
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
    run_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    state, resources = run_inputs
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
    run_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = run_inputs
    backend = resources.base.checkout / state.config.backend_path
    backend.rmdir()
    backend.symlink_to(state.config.repo_path, target_is_directory=True)
    collect = Mock()
    monkeypatch.setattr(node_module, "collect_static_context", collect)

    with pytest.raises(EnvironmentSetupError, match="stay inside"):
        static_analyze(state, Runtime(context=resources))

    collect.assert_not_called()


def test_missing_runtime_context_is_an_explicit_error(
    run_inputs: tuple[RunState, RunResources],
) -> None:
    state, _ = run_inputs
    with pytest.raises(ValueError, match="must be supplied"):
        validate_resources(state, None)


def test_inspection_failure_propagates_without_fabricating_context(
    run_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = run_inputs
    error = MigrationInspectionError("Cannot load the migration graph.")
    monkeypatch.setattr(node_module, "collect_static_context", Mock(side_effect=error))

    with pytest.raises(MigrationInspectionError) as raised:
        static_analyze(state, Runtime(context=resources))

    assert raised.value is error
    assert state.static_context is None
