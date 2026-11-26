"""Qualify one proposed HTTP scenario against a seeded old-release snapshot."""

from skewguard.config import RunConfig
from skewguard.exec import diff
from skewguard.exec.db import DatabaseConnection, temporary_clone
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import SUPPORTED_HTTP_METHODS, run_http_scenario
from skewguard.exec.scenarios import Scenario
from skewguard.exec.types import Cell, ComparisonIssue, ComparisonIssueKind, Outcome


def baseline_rejection_reason(scenario: Scenario, outcome: Outcome) -> str | None:
    """Give feedback on an exception, failed HTTP status, or absent selected key.

    Return None when the response provides a usable HTTP baseline.
    """
    if outcome.scenario_id != scenario.scenario_id or outcome.status == "ok":
        raise ValueError("qualification requires matching HTTP scenario evidence")
    
    # http baseline check
    if outcome.status == "exception":
        return f"{outcome.exception_type}: {outcome.exception_message}"
    
    if not diff.baseline_is_usable(outcome):
        return f"Baseline returned HTTP {outcome.http_status}"
    
    missing = diff.missing_baseline_fields(outcome, scenario.comparison_policy())
    if missing:
        return "baseline response lacks keys: " + ", ".join(missing)
    
    return None


def qualify_scenario(
    config: RunConfig,
    base: PythonEnvironment,
    old_snapshot: DatabaseConnection,
    *,
    settings_module: str,
    scenario: Scenario,
) -> Outcome | ComparisonIssue:
    """Return observed A0D0 evidence on acceptance or a coverage issue on rejection.

    Infrastructure failures propagate to the caller. Each attempt starts with
    the same seed data, including attempts that commit writes or return errors.
    """
    if scenario.method not in SUPPORTED_HTTP_METHODS:
        return ComparisonIssue(
            cell=Cell.A0D0,
            scenario_id=scenario.scenario_id,
            kind=ComparisonIssueKind.PROBE_INPUT_UNAVAILABLE,
            message=f"The HTTP executor does not support {scenario.method}.",
        )
    with temporary_clone(config, old_snapshot) as database:
        outcome = run_http_scenario(
            config, base, database, settings_module=settings_module, scenario=scenario
        )
    reason = baseline_rejection_reason(scenario, outcome)
    if reason is None:
        return outcome
    return ComparisonIssue(
        cell=Cell.A0D0,
        scenario_id=scenario.scenario_id,
        kind=(
            ComparisonIssueKind.PROBE_INPUT_UNAVAILABLE
            if diff.probe_input_is_unavailable(outcome)
            else ComparisonIssueKind.BASELINE_FAILED
        ),
        message=reason,
        outcome=outcome,
    )
