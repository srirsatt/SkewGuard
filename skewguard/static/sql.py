"""Generate SQL context on disposable schema copies, separate from deploy verdicts."""

import json
import subprocess
from pathlib import Path

from skewguard.config import RunConfig
from skewguard.exec import db, runner
from skewguard.exec.envs import (
    PythonEnvironment,
    backend_directory,
    validate_environment,
)
from skewguard.static.migrations import compare_inventories
from skewguard.static.types import (
    AnalysisIssue,
    MigrationAnalysis,
    MigrationRecord,
    MigrationSQL,
    SQLExtraction,
)

_WORKER_PATH = Path(__file__).with_name("_sql_worker.py")


class SQLExtractionError(RuntimeError):
    """The SQL worker could not return valid collection evidence."""


def validate_analysis(analysis: MigrationAnalysis) -> None:
    """Reject altered selections before preparing schema or executing migrations."""
    if compare_inventories(analysis.base, analysis.head) != analysis:
        raise ValueError("migration analysis conflicts with its inventories")


def unavailable_sql(
    records: list[MigrationRecord],
    detail: str,
    *,
    issues: list[AnalysisIssue],
) -> SQLExtraction:
    """Keep whole-stage setup failures visible as extraction gaps."""
    return SQLExtraction(
        results=[
            MigrationSQL(migration=record.key, status="unavailable", detail=detail)
            for record in records
        ],
        issues=[
            *issues,
            *[
                AnalysisIssue(
                    kind="sql_unavailable",
                    migration=record.key,
                    source=record.source,
                    message=detail,
                )
                for record in records
            ],
        ],
    )


def run_sql_worker(
    config: RunConfig,
    head: PythonEnvironment,
    inspection_db: db.DatabaseConnection,
    records: list[MigrationRecord],
    *,
    settings_module: str,
) -> SQLExtraction:
    """Collect SQL on a run-owned database that may advance between migrations."""
    validate_environment(head)
    backend = backend_directory(config, head.checkout)
    payload = json.loads(runner.migration_input(config, inspection_db, settings_module))
    payload["migrations"] = [record.key.model_dump() for record in records]
    try:
        result = subprocess.run(
            [
                str(head.python),
                "-I",
                str(_WORKER_PATH),
                str(backend),
                str(head.checkout),
            ],
            cwd=backend,
            env=runner.migration_environment(),
            input=json.dumps(payload),
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "SQL worker failed").strip()
        raise SQLExtractionError(
            runner.redact_database_password(detail, inspection_db)
        ) from error
    except subprocess.TimeoutExpired as error:
        raise SQLExtractionError(
            f"SQL worker timed out after {config.timeout_seconds}s"
        ) from error
    except OSError as error:
        raise SQLExtractionError(f"could not start SQL worker: {error}") from error
    try:
        extraction = SQLExtraction.model_validate_json(result.stdout)
        if [item.migration for item in extraction.results] != [
            record.key for record in records
        ]:
            raise ValueError("SQL worker returned an unexpected migration sequence")
    except ValueError as error:
        raise SQLExtractionError(
            "SQL worker returned invalid collection evidence"
        ) from error
    # Redact decoded strings so quotes/newlines in passwords cannot evade JSON escaping.
    return SQLExtraction(
        results=[
            item.model_copy(
                update={
                    "sql": runner.redact_database_password(item.sql, inspection_db)
                    if item.sql is not None
                    else None,
                    "detail": runner.redact_database_password(
                        item.detail, inspection_db
                    )
                    if item.detail is not None
                    else None,
                }
            )
            for item in extraction.results
        ],
        issues=[
            issue.model_copy(
                update={
                    "message": runner.redact_database_password(
                        issue.message, inspection_db
                    )
                }
            )
            for issue in extraction.issues
        ],
    )


def collect_sql(
    config: RunConfig,
    base: PythonEnvironment,
    head: PythonEnvironment,
    server: db.DatabaseConnection,
    analysis: MigrationAnalysis,
    *,
    settings_module: str,
) -> SQLExtraction:
    """Prepare an untouched base schema and collect SQL on its disposable clone.

    Validate the analysis. With no selected migrations, preserve its issues and
    return empty results without database work. Otherwise clone an empty database,
    apply base migrations, clone that snapshot, and call run_sql_worker with head.
    Retain metadata issues alongside SQL issues. Convert known setup/worker errors
    into unavailable_sql results, redacting the server password. Container lifetime
    belongs to the caller; the original base snapshot must never receive head work.
    """
    raise NotImplementedError("SQL collection orchestration is not implemented")
