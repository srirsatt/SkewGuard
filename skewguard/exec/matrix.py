"""Collect probe evidence across application-version and database-state cells."""

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection, clone_database
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import (
    run_read_probes, run_write_probes, run_committed_writes, seed_database
)
from skewguard.exec.types import Cell, CellResult, Outcome
from skewguard.exec.runner import run_migrations



def collect_cell(
    config: RunConfig,
    environment: PythonEnvironment,
    database: DatabaseConnection,
    *,
    cell: Cell,
    settings_module: str,
    setup_outcomes: list[Outcome] | None = None,
) -> CellResult:
    """Record setup evidence and independent read/rolled-back-write outcomes."""
    reads = run_read_probes(
        config, environment, database, settings_module=settings_module
    )
    writes = run_write_probes(
        config, environment, database, settings_module=settings_module
    )
    return CellResult(cell=cell, outcomes=[*(setup_outcomes or []), *reads, *writes])


def run_matrix(
    config: RunConfig,
    base: PythonEnvironment,
    head: PythonEnvironment,
    server: DatabaseConnection,
    *,
    settings_module: str,
    seed_fixture: str | None = None,
) -> list[CellResult]:
    """Run all four cells using independent clones of untouched snapshots.
    """
    old_snapshot = clone_database(config, server)
    run_migrations(config, base, old_snapshot, settings_module=settings_module)

    if seed_fixture is not None:
        seed_database(
            config,
            base,
            old_snapshot,
            settings_module=settings_module,
            fixture=seed_fixture,
        )
    baseline_db = clone_database(config, server, source=old_snapshot.dbname)
    results = [
        collect_cell(
            config, base, baseline_db, cell=Cell.A0D0, settings_module=settings_module,
        )
    ]

    migrated_snapshot = clone_database(config, server, source=old_snapshot.dbname)
    run_migrations(config, head, migrated_snapshot, settings_module=settings_module)

    for cell, environment in (
        (Cell.A0D1, base),
        (Cell.A1D1, head),
        (Cell.A0D1W, base),
    ):
        database = clone_database(config, server, source=migrated_snapshot.dbname)

        committed: list[Outcome] = []
        if cell == Cell.A0D1W:
            committed = run_committed_writes(
                config, head, database, settings_module=settings_module
            )
        results.append(
            collect_cell(
                config,
                environment,
                database,
                cell=cell,
                settings_module=settings_module,
                setup_outcomes=committed,
            )
        )

    return results



