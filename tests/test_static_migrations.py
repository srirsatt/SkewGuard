"""Validate migration worker boundaries and conservative inventory comparison."""

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from skewguard.config import RunConfig
from skewguard.exec.envs import PythonEnvironment, environment_python
from skewguard.static import migrations
from skewguard.static.migrations import (
    MigrationInspectionError,
    compare_inventories,
    inspect_migrations,
    select_new_migrations,
)
from skewguard.static.types import (
    AnalysisIssue,
    MigrationAnalysis,
    MigrationInventory,
    MigrationKey,
    MigrationOperation,
    MigrationRecord,
)


def record(
    name: str,
    *,
    app: str = "catalog",
    parents: list[MigrationKey] | None = None,
    digest: str | None = "a" * 64,
) -> MigrationRecord:
    return MigrationRecord(
        key=MigrationKey(app_label=app, name=name),
        dependencies=parents or [],
        operations=[
            MigrationOperation(index=0, kind="CreateModel", model_name="Product")
        ],
        file_sha256=digest,
    )


def test_select_additions_preserves_head_graph_order_and_input_records() -> None:
    old = record("0001_initial")
    # Filename order is deliberately different from dependency order.
    first = record("0003_parent", parents=[old.key])
    second = record("0002_child", parents=[first.key])
    head = [old, first, second]
    original = deepcopy(head)

    selected = select_new_migrations([old], head)

    assert selected == [first, second]
    assert head == original
    assert selected[0] is first


def test_same_migration_name_in_a_new_app_is_still_an_addition() -> None:
    old = record("0001_initial")
    new_app = record("0001_initial", app="writers", parents=[old.key])

    assert select_new_migrations([old], [old, new_app]) == [new_app]


@pytest.mark.parametrize("empty", [False, True])
def test_unchanged_and_empty_inventories_have_no_additions(empty: bool) -> None:
    records = [] if empty else [record("0001_initial")]

    assert select_new_migrations(records, records) == []


def test_selection_rejects_duplicate_or_misordered_input() -> None:
    old = record("0001_initial")
    child = record("0002_change", parents=[old.key])

    with pytest.raises(ValueError, match="duplicate migration"):
        select_new_migrations([old, old], [old])
    with pytest.raises(ValueError, match="dependencies must precede"):
        select_new_migrations([old], [child, old])


@pytest.mark.parametrize(
    "change", ["removed", "edited", "metadata", "replacement", "missing_hash"]
)
def test_history_gaps_are_issues_instead_of_guessed_additions(change: str) -> None:
    old = record("0001_initial")
    base = MigrationInventory(migrations=[old])
    if change == "removed":
        head = MigrationInventory(migrations=[])
        expected = "removed_migration"
    elif change == "edited":
        head = MigrationInventory(
            migrations=[old.model_copy(update={"file_sha256": "b" * 64})]
        )
        expected = "rewritten_migration"
    elif change == "metadata":
        altered = old.model_copy(
            update={
                "operations": [
                    MigrationOperation(
                        index=0, kind="DeleteModel", model_name="Product"
                    )
                ]
            }
        )
        head = MigrationInventory(migrations=[altered])
        expected = "rewritten_migration"
    elif change == "replacement":
        replacement = record("0001_squashed").model_copy(update={"replaces": [old.key]})
        head = MigrationInventory(migrations=[replacement])
        expected = "replacement_migration"
    else:
        head = MigrationInventory(
            migrations=[old.model_copy(update={"file_sha256": None})]
        )
        expected = "source_unavailable"

    comparison = compare_inventories(base, head)

    assert comparison.new_migrations == []
    assert expected in [issue.kind for issue in comparison.issues]
    with pytest.raises(ValueError):
        select_new_migrations(base.migrations, head.migrations)


def test_unchanged_replacement_history_still_requires_explicit_handling() -> None:
    replaced = MigrationKey(app_label="catalog", name="0001_original")
    squashed = record("0001_squashed").model_copy(update={"replaces": [replaced]})
    inventory = MigrationInventory(migrations=[squashed])

    comparison = compare_inventories(inventory, inventory)

    assert comparison.new_migrations == []
    assert [issue.kind for issue in comparison.issues] == ["replacement_migration"]


def test_comparison_preserves_inventory_issues_and_roundtrips_all_evidence() -> None:
    old = record("0001_initial")
    issue = AnalysisIssue(
        kind="source_unavailable",
        migration=old.key,
        message="Line coordinates unavailable.",
    )
    base = MigrationInventory(migrations=[old], issues=[issue])
    new = record("0002_custom", parents=[old.key]).model_copy(
        update={
            "operations": [MigrationOperation(index=0, kind="CustomBackfill")],
        }
    )
    opaque = AnalysisIssue(
        kind="unsupported_operation",
        migration=new.key,
        operation_index=0,
        message="Custom operation.",
    )
    head = MigrationInventory(migrations=[old, new], issues=[issue, opaque])

    comparison = compare_inventories(base, head)

    assert comparison.new_migrations == [new]
    assert comparison.issues == [issue, opaque]
    assert (
        MigrationAnalysis.model_validate_json(comparison.model_dump_json())
        == comparison
    )


@pytest.fixture
def inspection_inputs(tmp_path: Path) -> tuple[RunConfig, PythonEnvironment]:
    checkout = tmp_path / "release"
    (checkout / "backend").mkdir(parents=True)
    venv = checkout / ".skewguard-venv"
    python = environment_python(venv)
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("", encoding="utf-8")
    return (
        RunConfig(
            repo_path=tmp_path,
            base_ref="base",
            head_ref="head",
            backend_path=Path("backend"),
            timeout_seconds=7,
        ),
        PythonEnvironment(checkout=checkout, venv=venv, python=python),
    )


def test_launcher_uses_release_interpreter_protocol_and_timeout(
    inspection_inputs: tuple[RunConfig, PythonEnvironment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, environment = inspection_inputs
    expected = MigrationInventory(migrations=[record("0001_initial")])
    run = Mock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=expected.model_dump_json(), stderr=""
        )
    )
    monkeypatch.setattr(migrations.subprocess, "run", run)
    monkeypatch.setenv("PYTHONPATH", "inherited-selector")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "wrong.settings")

    assert (
        inspect_migrations(config, environment, settings_module="shop.settings")
        == expected
    )
    command = run.call_args.args[0]
    assert command[0] == str(environment.python)
    assert command[1] == "-I"
    assert command[2].endswith("_migration_worker.py")
    assert command[3:] == [
        str(environment.checkout / "backend"),
        str(environment.checkout),
    ]
    assert run.call_args.kwargs["cwd"] == environment.checkout / "backend"
    assert run.call_args.kwargs["timeout"] == 7
    assert run.call_args.kwargs["check"] is True
    assert json.loads(run.call_args.kwargs["input"]) == {
        "settings_module": "shop.settings"
    }
    assert "PYTHONPATH" not in run.call_args.kwargs["env"]
    assert "DJANGO_SETTINGS_MODULE" not in run.call_args.kwargs["env"]


@pytest.mark.parametrize(
    "settings", ["", "shop..settings", "shop/settings", "shop.settings;echo"]
)
def test_invalid_settings_fail_before_launch(
    inspection_inputs: tuple[RunConfig, PythonEnvironment],
    monkeypatch: pytest.MonkeyPatch,
    settings: str,
) -> None:
    config, environment = inspection_inputs
    run = Mock()
    monkeypatch.setattr(migrations.subprocess, "run", run)

    with pytest.raises(MigrationInspectionError, match="dotted Python module"):
        inspect_migrations(config, environment, settings_module=settings)
    run.assert_not_called()


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            subprocess.CalledProcessError(1, "python", stderr="graph load failed"),
            "graph load failed",
        ),
        (subprocess.TimeoutExpired("python", 7), "timed out after 7s"),
        (OSError("cannot execute interpreter"), "could not start"),
    ],
)
def test_worker_failures_are_inspection_errors(
    inspection_inputs: tuple[RunConfig, PythonEnvironment],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    message: str,
) -> None:
    config, environment = inspection_inputs
    monkeypatch.setattr(migrations.subprocess, "run", Mock(side_effect=error))

    with pytest.raises(MigrationInspectionError, match=message) as raised:
        inspect_migrations(config, environment, settings_module="shop.settings")
    assert raised.value.__cause__ is error


@pytest.mark.parametrize("output", ["not JSON", '{"migrations": [{"key": "bad"}]}'])
def test_invalid_worker_evidence_is_rejected(
    inspection_inputs: tuple[RunConfig, PythonEnvironment],
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    config, environment = inspection_inputs
    monkeypatch.setattr(
        migrations.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr=""
            )
        ),
    )

    with pytest.raises(MigrationInspectionError, match="invalid inventory"):
        inspect_migrations(config, environment, settings_module="shop.settings")
