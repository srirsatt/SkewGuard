"""Execute read probes and fixture inserts in a release's Django environment."""

import json
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import (
    PythonEnvironment,
    backend_directory,
    validate_environment,
)
from skewguard.exec.runner import (
    migration_environment,
    migration_input,
    redact_database_password,
)
from skewguard.exec.types import Outcome

_WORKER_PATH = Path(__file__).with_name("_probe_worker.py")


class ProbeExecutionError(RuntimeError):
    """The child could not return valid probe evidence."""


def _run_worker(
    config: RunConfig,
    environment: PythonEnvironment,
    database: DatabaseConnection,
    *,
    settings_module: str,
    operation: Literal["seed", "read"],
    fixture: str | None = None,
    model_labels: list[str] | None = None,
) -> list[Outcome]:
    validate_environment(environment)
    backend = backend_directory(config, environment.checkout)
    payload = json.loads(migration_input(config, database, settings_module))
    payload.update(
        operation=operation,
        fixture=fixture,
        model_labels=model_labels,
        row_limit=config.probe_row_limit,
    )
    try:
        result = subprocess.run(
            [str(environment.python), "-I", str(_WORKER_PATH), str(backend)],
            cwd=backend,
            env=migration_environment(),
            input=json.dumps(payload),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "probe worker failed"
        raise ProbeExecutionError(redact_database_password(detail, database)) from error
    except subprocess.TimeoutExpired as error:
        raise ProbeExecutionError(
            f"Django probe worker timed out after {config.timeout_seconds}s"
        ) from error
    except OSError as error:
        raise ProbeExecutionError(f"could not start probe worker: {error}") from error

    try:
        return TypeAdapter(list[Outcome]).validate_json(
            redact_database_password(result.stdout, database)
        )
    except ValueError as error:
        raise ProbeExecutionError("probe worker returned invalid evidence") from error


def seed_database(
    config: RunConfig,
    environment: PythonEnvironment,
    database: DatabaseConnection,
    *,
    settings_module: str,
    fixture: str,
) -> None:
    """Commit a Django fixture into the supplied sandbox database."""
    _run_worker(
        config,
        environment,
        database,
        settings_module=settings_module,
        operation="seed",
        fixture=fixture,
    )


def run_read_probes(
    config: RunConfig,
    environment: PythonEnvironment,
    database: DatabaseConnection,
    *,
    settings_module: str,
    model_labels: list[str] | None = None,
) -> list[Outcome]:
    """Collect validated read outcomes for selected models, or all installed models."""
    return _run_worker(
        config,
        environment,
        database,
        settings_module=settings_module,
        operation="read",
        model_labels=model_labels,
    )
