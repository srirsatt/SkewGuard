"""Collect probe evidence across application-version and database-state cells."""

from collections.abc import Sequence
from contextlib import nullcontext

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection, clone_database, temporary_clone
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import (
    ProbeExecutionError,
    run_committed_writes,
    run_http_scenario,
    run_read_probes,
    run_write_probes,
    seed_database,
)
from skewguard.exec.qualification import qualify_scenario
from skewguard.exec.runner import run_migrations
from skewguard.exec.scenarios import Scenario
from skewguard.exec.types import (
    Cell,
    CellResult,
    ComparisonIssue,
    ComparisonIssueKind,
    Outcome,
)


def run_http_batch(
    config: RunConfig,
    environment: PythonEnvironment,
    database: DatabaseConnection,
    scenarios: Sequence[Scenario],
    *,
    settings_module: str,
    persist_writes: bool = False,
) -> list[Outcome]:
    """Isolate independent requests, or accumulate the writes for a rollback check."""
    outcomes: list[Outcome] = []
    for scenario in scenarios:
        target = (
            nullcontext(database)
            if persist_writes
            else temporary_clone(config, database)
        )
        with target as db:
            outcomes.append(
                run_http_scenario(
                    config,
                    environment,
                    db,
                    settings_module=settings_module,
                    scenario=scenario,
                )
            )
    return outcomes


def run_rollback_http(
    config: RunConfig,
    writer: PythonEnvironment,
    reader: PythonEnvironment,
    database: DatabaseConnection,
    scenarios: Sequence[Scenario],
    *,
    settings_module: str,
) -> tuple[list[Outcome], list[Outcome]]:
    """Commit the writer's requests, then observe how the reader handles those rows."""
    writes = [s for s in scenarios if s.kind == "write"]
    reads = [s for s in scenarios if s.kind == "read"]
    written = run_http_batch(
        config,
        writer,
        database,
        writes,
        settings_module=settings_module,
        persist_writes=True,
    )
    observed = run_http_batch(
        config, reader, database, reads, settings_module=settings_module
    )
    return written, observed


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


def collect_baseline(
    config: RunConfig,
    base: PythonEnvironment,
    old_snapshot: DatabaseConnection,
    *,
    settings_module: str,
    required_rollback_writes: frozenset[str],
) -> tuple[CellResult, list[ComparisonIssue]]:
    """Qualify proposals and collect the equivalent old-release rollback controls."""
    baseline_db = clone_database(config, old_snapshot, source=old_snapshot.dbname)
    baseline = collect_cell(
        config, base, baseline_db, cell=Cell.A0D0, settings_module=settings_module
    )
    accepted: list[Scenario] = []
    issues: list[ComparisonIssue] = []
    http_baselines: list[Outcome] = []
    for scenario in config.scenarios:
        evidence = qualify_scenario(
            config,
            base,
            old_snapshot,
            settings_module=settings_module,
            scenario=scenario,
        )
        if isinstance(evidence, ComparisonIssue):
            issues.append(evidence)
        else:
            accepted.append(scenario)
            http_baselines.append(evidence)
    reads = [scenario for scenario in accepted if scenario.kind == "read"]
    rollback_issues: list[ComparisonIssue] = []
    lost_writes = required_rollback_writes.difference(
        s.scenario_id for s in accepted if s.kind == "write"
    )
    if lost_writes:
        rollback_issues.append(
            ComparisonIssue(
                cell=Cell.A0D1W,
                kind=ComparisonIssueKind.ROLLBACK_SETUP_FAILED,
                message="Previously qualified writes lost their baseline: "
                + ", ".join(sorted(lost_writes)),
            )
        )
    controls: list[Outcome] = []
    control_writes: list[Outcome] = []
    if reads:
        with temporary_clone(config, old_snapshot) as control:
            try:
                run_committed_writes(
                    config, base, control, settings_module=settings_module
                )
            except ProbeExecutionError as error:
                for read in reads:
                    rollback_issues.append(
                        ComparisonIssue(
                            cell=Cell.A0D1W,
                            kind=ComparisonIssueKind.ROLLBACK_SETUP_FAILED,
                            scenario_id=read.scenario_id,
                            message=f"Old-release control setup failed: {error}",
                        )
                    )
            else:
                control_writes, controls = run_rollback_http(
                    config,
                    base,
                    base,
                    control,
                    accepted,
                    settings_module=settings_module,
                )
    return CellResult(
        cell=Cell.A0D0,
        outcomes=[*baseline.outcomes, *http_baselines],
        qualified_scenarios=tuple(accepted),
        execution_issues=tuple(issues),
        rollback_controls=tuple(controls),
        rollback_writes=tuple(control_writes),
    ), rollback_issues


def run_matrix(
    config: RunConfig,
    base: PythonEnvironment,
    head: PythonEnvironment,
    server: DatabaseConnection,
    *,
    settings_module: str,
    seed_fixture: str | None = None,
    required_rollback_writes: frozenset[str] = frozenset(),
) -> list[CellResult]:
    """Run all four cells using independent clones of untouched snapshots."""
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
    baseline, rollback_issues = collect_baseline(
        config,
        base,
        old_snapshot,
        settings_module=settings_module,
        required_rollback_writes=required_rollback_writes,
    )
    results = [baseline]
    accepted = baseline.qualified_scenarios

    migrated_snapshot = clone_database(config, server, source=old_snapshot.dbname)
    run_migrations(config, head, migrated_snapshot, settings_module=settings_module)

    for cell, environment in (
        (Cell.A0D1, base),
        (Cell.A1D1, head),
        (Cell.A0D1W, base),
    ):
        database = clone_database(config, server, source=migrated_snapshot.dbname)

        committed: list[Outcome] = []
        http_writes: list[Outcome] = []
        if cell == Cell.A0D1W:
            committed = run_committed_writes(
                config, head, database, settings_module=settings_module
            )
            http_writes, http = run_rollback_http(
                config, head, base, database, accepted, settings_module=settings_module
            )
        else:
            http = run_http_batch(
                config,
                environment,
                migrated_snapshot,
                accepted,
                settings_module=settings_module,
            )
        probes = collect_cell(
            config,
            environment,
            database,
            cell=cell,
            settings_module=settings_module,
            setup_outcomes=committed,
        )
        results.append(
            CellResult(
                cell=cell,
                outcomes=[*probes.outcomes, *http],
                rollback_writes=tuple(http_writes),
                execution_issues=tuple(rollback_issues) if cell == Cell.A0D1W else (),
            )
        )

    return results
