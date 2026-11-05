"""Collect probe evidence across application-version and database-state cells."""

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import run_read_probes, run_write_probes
from skewguard.exec.types import Cell, CellResult, Outcome


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

    Build and optionally seed an old-schema snapshot. Probe its clone for A0D0.
    Migrate another clone with head code, then clone that migrated snapshot for
    A0D1, A1D1, and A0D1w. Commit head writes only in A0D1w before old-code probes.
    Preserve committed-write evidence. Return all four CellResults in Cell order.
    The caller owns the worktree, environment, and server lifetimes.
    """
    raise NotImplementedError("four-cell orchestration is not implemented")
