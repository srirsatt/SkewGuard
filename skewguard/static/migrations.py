"""Launch isolated metadata inspection and compare release migration inventories."""

import json
import subprocess
from pathlib import Path

from skewguard.config import RunConfig
from skewguard.exec.envs import (
    PythonEnvironment,
    backend_directory,
    validate_environment,
)
from skewguard.exec.runner import migration_environment
from skewguard.static.types import (
    AnalysisIssue,
    MigrationAnalysis,
    MigrationInventory,
    MigrationRecord,
)

_WORKER_PATH = Path(__file__).with_name("_migration_worker.py")


class MigrationInspectionError(RuntimeError):
    """The release could not provide a valid migration inventory."""


def inspect_migrations(
    config: RunConfig,
    environment: PythonEnvironment,
    *,
    settings_module: str,
) -> MigrationInventory:
    """Read one release's migration metadata with bounded subprocess execution."""
    validate_environment(environment)
    backend = backend_directory(config, environment.checkout)
    if not settings_module or not all(
        part.isidentifier() for part in settings_module.split(".")
    ):
        raise MigrationInspectionError(
            "settings_module must be a dotted Python module name"
        )
    try:
        result = subprocess.run(
            [
                str(environment.python),
                "-I",
                str(_WORKER_PATH),
                str(backend),
                str(environment.checkout),
            ],
            cwd=backend,
            env=migration_environment(),
            input=json.dumps({"settings_module": settings_module}),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "metadata worker failed").strip()
        raise MigrationInspectionError(
            f"Django metadata inspection failed: {detail}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise MigrationInspectionError(
            f"Django metadata inspection timed out after {config.timeout_seconds}s"
        ) from error
    except OSError as error:
        raise MigrationInspectionError(
            f"could not start metadata worker: {error}"
        ) from error
    try:
        return MigrationInventory.model_validate_json(result.stdout)
    except ValueError as error:
        raise MigrationInspectionError(
            "metadata worker returned an invalid inventory"
        ) from error


def history_issues(
    base: list[MigrationRecord],
    head: list[MigrationRecord],
) -> list[AnalysisIssue]:
    """Identify histories that cannot be interpreted as unchanged base plus additions.

    This first comparison supports ordinary append-only history. Replacements,
    removals, edits, and unverifiable shared sources are retained for explicit
    handling, rather than guessing which migrations would apply to a database.
    """
    new_by_key = {record.key: record for record in head}
    issues: list[AnalysisIssue] = []
    seen_replacements: set[str] = set()
    for record in [*base, *head]:
        identity = record.key.model_dump_json()
        if record.replaces and identity not in seen_replacements:
            issues.append(
                AnalysisIssue(
                    kind="replacement_migration",
                    migration=record.key,
                    source=record.source,
                    message="Replacement migration requires explicit "
                    "history comparison.",
                )
            )
            seen_replacements.add(identity)
    for old in base:
        new = new_by_key.get(old.key)
        if new is None:
            issues.append(
                AnalysisIssue(
                    kind="removed_migration",
                    migration=old.key,
                    source=old.source,
                    message="A migration present in base is missing from head.",
                )
            )
            continue
        old_metadata = old.model_dump(exclude={"source", "file_sha256"})
        new_metadata = new.model_dump(exclude={"source", "file_sha256"})
        if old_metadata != new_metadata or (
            old.file_sha256 is not None
            and new.file_sha256 is not None
            and old.file_sha256 != new.file_sha256
        ):
            issues.append(
                AnalysisIssue(
                    kind="rewritten_migration",
                    migration=old.key,
                    source=new.source,
                    message="An existing migration's source or declared "
                    "metadata changed.",
                )
            )
        elif old.file_sha256 is None or new.file_sha256 is None:
            issues.append(
                AnalysisIssue(
                    kind="source_unavailable",
                    migration=old.key,
                    source=new.source,
                    message="Cannot verify unchanged shared migration history "
                    "without both file hashes.",
                )
            )
    return issues


def select_new_migrations(
    base: list[MigrationRecord],
    head: list[MigrationRecord],
) -> list[MigrationRecord]:
    """Select new keys in head order after validating ordinary append-only history.

    Validate each list as a MigrationInventory. Reject history_issues with a
    ValueError, since a filename difference alone cannot resolve edited/removed/
    replacement history. Return head records whose full MigrationKey is absent
    from base. Keep their existing dependency order and do not mutate either list.
    """
    
    # Validate the supplied inventories, check history, and select new head records.

    MigrationInventory(migrations=base)
    MigrationInventory(migrations=head)

    problems = history_issues(base, head)
    if problems:
        messages = "; ".join(issue.message for issue in problems)
        raise ValueError(f"Cannot select new migrations: {messages}")

    base_keys = {migration.key for migration in base}

    return [
        migration
        for migration in head
        if migration.key not in base_keys
    ]


def compare_inventories(
    base: MigrationInventory,
    head: MigrationInventory,
) -> MigrationAnalysis:
    """Compare validated inventories and keep inspection/history issues visible.

    A history gap yields no selected additions until that history is resolved;
    callers must inspect issues rather than interpreting an empty list as safe.
    """
    history = history_issues(base.migrations, head.migrations)
    issues: list[AnalysisIssue] = []
    seen: set[str] = set()
    for issue in [*base.issues, *head.issues, *history]:
        identity = issue.model_dump_json()
        if identity not in seen:
            issues.append(issue)
            seen.add(identity)
    return MigrationAnalysis(
        base=base,
        head=head,
        new_migrations=(
            [] if history else select_new_migrations(base.migrations, head.migrations)
        ),
        issues=issues,
    )


def analyze_migrations(
    config: RunConfig,
    base: PythonEnvironment,
    head: PythonEnvironment,
    *,
    settings_module: str,
) -> MigrationAnalysis:
    """Inspect both releases without a database and return metadata, never verdicts."""
    old = inspect_migrations(config, base, settings_module=settings_module)
    new = inspect_migrations(config, head, settings_module=settings_module)
    return compare_inventories(old, new)
