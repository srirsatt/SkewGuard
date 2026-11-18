"""Recover typed evidence across connections without reviving runtime resources."""

# LangGraph's optional hooks expose unbound generics in public method types.
# pyright: reportUnknownMemberType=false

import importlib
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from pydantic import BaseModel, SecretStr

from skewguard.exec.envs import PythonEnvironment, environment_python
from skewguard.exec.probes import ProbeExecutionError
from skewguard.graph import session
from skewguard.graph.build import build_graph
from skewguard.graph.checkpoints import (
    CheckpointError,
    checkpoint_serializer,
    open_checkpoints,
    run_config,
)
from skewguard.graph.progress import stream_workflow
from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState
from skewguard.report.json_report import RunReport
from skewguard.static.migrations import compare_inventories
from skewguard.static.types import (
    CodeReferenceScan,
    MigrationInventory,
    SQLExtraction,
    StaticContext,
)

analysis_node = importlib.import_module("skewguard.graph.nodes.static_analyze")
execution_node = importlib.import_module("skewguard.graph.nodes.execute_matrix")
RUN_ID = "1" * 32


def context() -> StaticContext:
    inventory = MigrationInventory(migrations=[])
    return StaticContext(
        migrations=compare_inventories(inventory, inventory),
        sql=SQLExtraction(results=[]),
        base_code=CodeReferenceScan(references=[], sql_literals=[], issues=[]),
        head_code=CodeReferenceScan(references=[], sql_literals=[], issues=[]),
    )


def test_failed_execution_resumes_at_pinned_commits_with_fresh_resources(
    graph_inputs: tuple[RunState, RunResources],
    report_factory: Callable[[str], RunReport],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = graph_inputs
    evidence = report_factory("breaking").verification
    inspect = Mock(return_value=context())
    verify = Mock(side_effect=ProbeExecutionError("simulated process failure"))
    monkeypatch.setattr(analysis_node, "collect_static_context", inspect)
    monkeypatch.setattr(execution_node, "verify_matrix", verify)
    path = tmp_path / "checkpoints.sqlite"
    messages: list[str] = []

    with open_checkpoints(path, RUN_ID, create=True) as saver:
        workflow = build_graph(saver)
        with pytest.raises(ProbeExecutionError):
            stream_workflow(
                workflow,
                state,
                resources,
                config=run_config(RUN_ID),
                durable=True,
                on_progress=messages.append,
            )
        snapshot = workflow.get_state(run_config(RUN_ID))
        assert snapshot.next == ("execute_matrix",)
        assert RunState.model_validate(snapshot.values).static_context == context()

    fresh_environments: list[PythonEnvironment] = []
    for environment in (resources.base, resources.head):
        checkout = environment.checkout.with_name(environment.checkout.name + "-fresh")
        environment.checkout.rename(checkout)
        venv = checkout / ".skewguard-venv"
        fresh_environments.append(
            PythonEnvironment(
                checkout=checkout,
                venv=venv,
                python=environment_python(venv),
            )
        )
    fresh = replace(
        resources,
        checkouts=resources.checkouts.model_copy(
            update={
                "base": fresh_environments[0].checkout,
                "head": fresh_environments[1].checkout,
            }
        ),
        base=fresh_environments[0],
        head=fresh_environments[1],
        server=resources.server.model_copy(
            update={
                "password": SecretStr("fresh-runtime-credential"),
                "port": 6543,
            }
        ),
    )
    prepared = Mock()

    @contextmanager
    def rebuild(*args: object, **kwargs: object) -> Generator[RunResources, None, None]:
        prepared(*args, **kwargs)
        yield fresh

    monkeypatch.setattr(session, "prepared_resources", rebuild)
    verify.side_effect = None
    verify.return_value = evidence

    result = session.resume_session(path, RUN_ID, on_progress=messages.append)

    assert result.verification == evidence
    assert result.summary.confirmed == 2
    inspect.assert_called_once()
    assert verify.call_count == 2
    verify.assert_called_with(
        state.config,
        fresh.base,
        fresh.head,
        fresh.server,
        settings_module=state.settings_module,
        seed_fixture=state.seed_fixture,
    )
    assert prepared.call_args.args == (state.config,)
    assert prepared.call_args.kwargs["pins"] == (state.base_sha, state.head_sha)
    assert messages == [
        "Starting: static analysis",
        "Completed: static analysis",
        "Starting: matrix and reproduction checks",
        "Starting: matrix and reproduction checks",
        "Completed: matrix and reproduction checks",
        "Starting: report generation",
        "Completed: report generation",
    ]
    with open_checkpoints(path, RUN_ID, create=False) as saver:
        snapshot = build_graph(saver).get_state(run_config(RUN_ID))
        assert not snapshot.next
        assert (
            session.completed_report(RunState.model_validate(snapshot.values)) == result
        )
    raw = path.read_bytes()
    assert resources.server.password.get_secret_value().encode() not in raw
    assert fresh.server.password.get_secret_value().encode() not in raw
    assert str(resources.base.venv).encode() not in raw

    monkeypatch.setattr(
        session,
        "prepared_resources",
        Mock(side_effect=AssertionError("must not prepare")),
    )
    assert session.resume_session(path, RUN_ID) == result
    assert verify.call_count == 2


def test_completed_checkpoint_survives_an_independent_python_process(
    graph_inputs: tuple[RunState, RunResources],
    report_factory: Callable[[str], RunReport],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = graph_inputs
    monkeypatch.setattr(
        analysis_node, "collect_static_context", Mock(return_value=context())
    )
    monkeypatch.setattr(
        execution_node,
        "verify_matrix",
        Mock(return_value=report_factory("safe").verification),
    )
    path = tmp_path / "checkpoints.sqlite"
    with open_checkpoints(path, RUN_ID, create=True) as saver:
        final = stream_workflow(
            build_graph(saver),
            state,
            resources,
            config=run_config(RUN_ID),
            durable=True,
        )
    code = (
        "import sys\nfrom pathlib import Path\n"
        "from skewguard.graph.session import resume_session\n"
        "result = resume_session(Path(sys.argv[1]), sys.argv[2])\n"
        "print(result.summary.status)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(path), RUN_ID],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert result.stdout.strip() == "no_break_detected"
    assert final.report is not None


def test_different_run_ids_keep_independent_state(
    graph_inputs: tuple[RunState, RunResources],
    report_factory: Callable[[str], RunReport],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = graph_inputs
    monkeypatch.setattr(
        analysis_node, "collect_static_context", Mock(return_value=context())
    )
    monkeypatch.setattr(
        execution_node,
        "verify_matrix",
        Mock(return_value=report_factory("safe").verification),
    )
    path = tmp_path / "checkpoints.sqlite"
    for identifier, settings in (
        (RUN_ID, "shop.settings"),
        ("2" * 32, "other.settings"),
    ):
        selected = state.model_copy(update={"settings_module": settings})
        with open_checkpoints(path, identifier, create=True) as saver:
            stream_workflow(
                build_graph(saver),
                selected,
                resources,
                config=run_config(identifier),
                durable=True,
            )
    assert (
        session.resume_session(path, RUN_ID).context.settings_module == "shop.settings"
    )
    assert (
        session.resume_session(path, "2" * 32).context.settings_module
        == "other.settings"
    )
    with pytest.raises(CheckpointError, match="not found"):
        session.resume_session(path, "3" * 32)


def test_existing_run_id_cannot_be_reused_for_new_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph_inputs: tuple[RunState, RunResources],
) -> None:
    state, _ = graph_inputs
    path = tmp_path / "checkpoints.sqlite"
    with open_checkpoints(path, RUN_ID, create=True) as saver:
        workflow = build_graph(saver)
        workflow.update_state(run_config(RUN_ID), state.model_dump())
    prepared = Mock(side_effect=AssertionError("must not prepare"))
    monkeypatch.setattr(session, "prepared_resources", prepared)
    with pytest.raises(CheckpointError, match="already exists"):
        session.run_session(
            state.config,
            settings_module=state.settings_module,
            checkpoint_path=path,
            run_id=RUN_ID,
        )
    prepared.assert_not_called()


def test_foreign_sqlite_files_are_not_modified(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE app_data (value TEXT)")
        connection.execute("INSERT INTO app_data VALUES ('preserve')")
    with pytest.raises(CheckpointError, match="not a SkewGuard"):
        with open_checkpoints(path, RUN_ID, create=True):
            pass
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM app_data").fetchall() == [
            ("preserve",)
        ]
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("app_data",)]


def test_missing_store_and_unsupported_versions_are_explicit(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite"
    with pytest.raises(CheckpointError, match="does not exist"):
        with open_checkpoints(path, RUN_ID, create=False):
            pass
    assert not path.exists()
    with open_checkpoints(path, RUN_ID, create=True):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE skewguard_store SET version=99")
    with pytest.raises(CheckpointError, match="unsupported"):
        with open_checkpoints(path, RUN_ID, create=False):
            pass


def test_run_lock_rejects_a_second_process_and_is_released_after_exit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "checkpoints.sqlite"
    code = (
        "import os, sys\nfrom pathlib import Path\n"
        "from skewguard.graph.checkpoints import open_checkpoints, CheckpointError\n"
        "try:\n"
        " with open_checkpoints(Path(sys.argv[1]), sys.argv[2], create=True):\n"
        "  os._exit(7)\n"
        "except CheckpointError:\n sys.exit(0)\n"
    )
    with open_checkpoints(path, RUN_ID, create=True):
        blocked = subprocess.run(
            [sys.executable, "-c", code, str(path), RUN_ID], timeout=20
        )
        assert blocked.returncode == 0
    abandoned = subprocess.run(
        [sys.executable, "-c", code, str(path), RUN_ID], timeout=20
    )
    assert abandoned.returncode == 7
    with open_checkpoints(path, RUN_ID, create=False):
        pass


@pytest.mark.parametrize("identifier", ["../outside", "short", "A" * 32, "1" * 33])
def test_invalid_identifiers_cannot_create_stores(
    tmp_path: Path, identifier: str
) -> None:
    path = tmp_path / "new" / "checkpoints.sqlite"
    with pytest.raises(CheckpointError, match="run ID"):
        with open_checkpoints(path, identifier, create=True):
            pass
    assert not path.parent.exists()


class UnregisteredModel(BaseModel):
    value: str


def test_serializer_does_not_revive_unregistered_models() -> None:
    serializer = checkpoint_serializer()
    restored = serializer.loads_typed(
        serializer.dumps_typed(UnregisteredModel(value="x"))
    )
    assert not isinstance(restored, UnregisteredModel)


def test_invocation_step_limit_bounds_an_accidental_cycle(
    graph_inputs: tuple[RunState, RunResources],
) -> None:
    state, _ = graph_inputs
    visits: list[int] = []

    def cycle(state: RunState) -> dict[str, str | None]:
        visits.append(1)
        return {"seed_fixture": state.seed_fixture}

    builder = StateGraph(RunState)
    builder.add_node("cycle", cycle)
    builder.add_edge(START, "cycle")
    builder.add_edge("cycle", "cycle")
    builder.add_edge("cycle", END)
    with pytest.raises(GraphRecursionError):
        builder.compile().invoke(state, config=run_config(RUN_ID), version="v2")
    assert 0 < len(visits) <= 100
