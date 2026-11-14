"""Validate SQL coverage, subprocess boundaries, and disposable-schema orchestration."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import SecretStr, ValidationError

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import PythonEnvironment, environment_python
from skewguard.static import sql
from skewguard.static.migrations import compare_inventories
from skewguard.static.sql import SQLExtractionError, collect_sql, run_sql_worker
from skewguard.static.types import (
    AnalysisIssue,
    MigrationAnalysis,
    MigrationInventory,
    MigrationKey,
    MigrationRecord,
    MigrationSQL,
    SQLExtraction,
)


def analysis(*, changed: bool = True) -> MigrationAnalysis:
    old = MigrationRecord(
        key=MigrationKey(app_label="catalog", name="0001_initial"),
        dependencies=[],
        operations=[],
        file_sha256="a" * 64,
    )
    new = MigrationRecord(
        key=MigrationKey(app_label="catalog", name="0002_change"),
        dependencies=[old.key],
        operations=[],
        file_sha256="b" * 64,
    )
    return compare_inventories(
        MigrationInventory(migrations=[old]),
        MigrationInventory(migrations=[old, new] if changed else [old]),
    )


@pytest.mark.parametrize(
    ("status", "text", "detail"),
    [
        ("collected", "", None),
        ("partial", "-- Python omitted", "Python omitted"),
        ("unavailable", None, "failed"),
    ],
)
def test_sql_coverage_records_roundtrip(
    status: str, text: str | None, detail: str | None
) -> None:
    result = MigrationSQL.model_validate(
        {
            "migration": {"app_label": "catalog", "name": "0002_change"},
            "status": status,
            "sql": text,
            "detail": detail,
        }
    )
    assert MigrationSQL.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    ("status", "text", "detail"),
    [
        ("collected", None, None),
        ("collected", "SELECT 1;", "failed"),
        ("partial", "", None),
        ("partial", None, "missing"),
        ("unavailable", None, None),
        ("unavailable", "SELECT 1;", "failed"),
    ],
)
def test_sql_coverage_rejects_conflicting_evidence(
    status: str, text: str | None, detail: str | None
) -> None:
    with pytest.raises(ValidationError):
        MigrationSQL.model_validate(
            {
                "migration": {"app_label": "catalog", "name": "0002_change"},
                "status": status,
                "sql": text,
                "detail": detail,
            }
        )


def test_duplicate_sql_migration_records_are_rejected() -> None:
    result = MigrationSQL(
        migration=analysis().new_migrations[0].key, status="collected", sql=""
    )
    with pytest.raises(ValidationError, match="duplicate migration"):
        SQLExtraction(results=[result, result])


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[RunConfig, PythonEnvironment, DatabaseConnection]:
    checkout = tmp_path / "head"
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
        DatabaseConnection(
            host="localhost",
            port=5432,
            username="test",
            password=SecretStr("quoted 'password'\nvalue"),
            dbname="postgres",
        ),
    )


def test_worker_protocol_redacts_decoded_sql_and_errors(
    inputs: tuple[RunConfig, PythonEnvironment, DatabaseConnection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, head, database = inputs
    records = analysis().new_migrations
    password = database.password.get_secret_value()
    response = SQLExtraction(
        results=[
            MigrationSQL(
                migration=records[0].key,
                status="partial",
                sql=f"SELECT '{password}';",
                detail=f"error {password}",
            )
        ],
        issues=[AnalysisIssue(kind="sql_partial", message=f"issue {password}")],
    )
    run = Mock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=response.model_dump_json(), stderr=""
        )
    )
    monkeypatch.setattr(sql.subprocess, "run", run)

    result = run_sql_worker(
        config, head, database, records, settings_module="shop.settings"
    )

    assert password not in (result.results[0].sql or "")
    assert password not in (result.results[0].detail or "")
    assert "[redacted]" in result.issues[0].message
    assert run.call_args.args[0][0] == str(head.python)
    assert run.call_args.args[0][1] == "-I"
    assert run.call_args.kwargs["timeout"] == 7
    payload = json.loads(run.call_args.kwargs["input"])
    assert payload["database"]["PASSWORD"] == password
    assert payload["migrations"] == [records[0].key.model_dump()]


@pytest.mark.parametrize("output", ["invalid JSON", '{"results": [], "issues": []}'])
def test_worker_invalid_or_missing_results_are_rejected(
    inputs: tuple[RunConfig, PythonEnvironment, DatabaseConnection],
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    config, head, database = inputs
    monkeypatch.setattr(
        sql.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr=""
            )
        ),
    )
    with pytest.raises(SQLExtractionError, match="invalid collection"):
        run_sql_worker(
            config,
            head,
            database,
            analysis().new_migrations,
            settings_module="shop.settings",
        )


@pytest.mark.parametrize("failure", ["exit", "timeout", "launch"])
def test_worker_failures_are_separate_from_compatibility(
    inputs: tuple[RunConfig, PythonEnvironment, DatabaseConnection],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    config, head, database = inputs
    error = {
        "exit": subprocess.CalledProcessError(
            1, "python", stderr=f"failure {database.password.get_secret_value()}"
        ),
        "timeout": subprocess.TimeoutExpired("python", 7),
        "launch": OSError("cannot start"),
    }[failure]
    monkeypatch.setattr(sql.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(SQLExtractionError) as raised:
        run_sql_worker(
            config,
            head,
            database,
            analysis().new_migrations,
            settings_module="shop.settings",
        )
    assert raised.value.__cause__ is error
    assert database.password.get_secret_value() not in str(raised.value)


def test_empty_selection_skips_database_preparation(
    inputs: tuple[RunConfig, PythonEnvironment, DatabaseConnection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, head, server = inputs
    clone = Mock(side_effect=AssertionError("must not clone"))
    monkeypatch.setattr(sql.db, "clone_database", clone)

    assert collect_sql(
        config,
        head,
        head,
        server,
        analysis(changed=False),
        settings_module="shop.settings",
    ) == SQLExtraction(results=[])
    clone.assert_not_called()


def test_orchestration_preserves_base_snapshot_and_issues(
    inputs: tuple[RunConfig, PythonEnvironment, DatabaseConnection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, head, server = inputs
    base = head.model_copy(update={"checkout": config.repo_path / "base"})
    metadata = analysis()
    snapshot = server.model_copy(update={"dbname": "base_snapshot"})
    inspection = server.model_copy(update={"dbname": "inspection"})
    clone = Mock(side_effect=[snapshot, inspection])
    migrate = Mock()
    extraction = SQLExtraction(
        results=[
            MigrationSQL(
                migration=metadata.new_migrations[0].key,
                status="collected",
                sql="SELECT 1;",
            )
        ]
    )
    worker = Mock(return_value=extraction)
    monkeypatch.setattr(sql.db, "clone_database", clone)
    monkeypatch.setattr(sql.runner, "run_migrations", migrate)
    monkeypatch.setattr(sql, "run_sql_worker", worker)

    result = collect_sql(
        config, base, head, server, metadata, settings_module="shop.settings"
    )

    assert result == extraction
    assert clone.call_args_list[1].kwargs == {"source": "base_snapshot"}
    assert migrate.call_args.args == (config, base, snapshot)
    assert worker.call_args.args == (config, head, inspection, metadata.new_migrations)


def test_setup_error_is_retained_as_an_sql_gap(
    inputs: tuple[RunConfig, PythonEnvironment, DatabaseConnection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, head, server = inputs
    monkeypatch.setattr(
        sql.db,
        "clone_database",
        Mock(side_effect=sql.db.DatabaseSetupError("setup failed")),
    )

    result = collect_sql(
        config, head, head, server, analysis(), settings_module="shop.settings"
    )

    assert result.results[0].status == "unavailable"
    assert "setup failed" in (result.results[0].detail or "")
    assert result.issues[0].kind == "sql_unavailable"


def test_altered_selection_is_rejected_before_database_work(
    inputs: tuple[RunConfig, PythonEnvironment, DatabaseConnection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, head, server = inputs
    clone = Mock()
    monkeypatch.setattr(sql.db, "clone_database", clone)
    altered = analysis().model_copy(update={"new_migrations": []})

    with pytest.raises(ValueError, match="conflicts"):
        collect_sql(
            config, head, head, server, altered, settings_module="shop.settings"
        )
    clone.assert_not_called()


def test_empty_selection_keeps_history_issues_without_database_work(
    inputs: tuple[RunConfig, PythonEnvironment, DatabaseConnection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, head, server = inputs
    metadata = compare_inventories(analysis().base, MigrationInventory(migrations=[]))
    clone = Mock(side_effect=AssertionError("must not clone"))
    monkeypatch.setattr(sql.db, "clone_database", clone)

    result = collect_sql(
        config, head, head, server, metadata, settings_module="shop.settings"
    )

    assert result.results == []
    assert result.issues == metadata.issues
    assert result.issues[0].kind == "removed_migration"
    clone.assert_not_called()


def test_metadata_and_worker_issues_are_both_retained(
    inputs: tuple[RunConfig, PythonEnvironment, DatabaseConnection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, head, server = inputs
    original = analysis()
    metadata_issue = AnalysisIssue(
        kind="source_unavailable",
        migration=original.new_migrations[0].key,
        message="Operation source line unavailable.",
    )
    metadata = compare_inventories(
        original.base, original.head.model_copy(update={"issues": [metadata_issue]})
    )
    worker_issue = AnalysisIssue(kind="sql_partial", message="SQL coverage incomplete.")
    extraction = SQLExtraction(
        results=[
            MigrationSQL(
                migration=metadata.new_migrations[0].key,
                status="partial",
                sql="-- omitted",
                detail="SQL coverage incomplete.",
            )
        ],
        issues=[worker_issue],
    )
    monkeypatch.setattr(sql.db, "clone_database", Mock(return_value=server))
    monkeypatch.setattr(sql.runner, "run_migrations", Mock())
    monkeypatch.setattr(sql, "run_sql_worker", Mock(return_value=extraction))

    result = collect_sql(
        config, head, head, server, metadata, settings_module="shop.settings"
    )

    assert result.issues == [metadata_issue, worker_issue]
