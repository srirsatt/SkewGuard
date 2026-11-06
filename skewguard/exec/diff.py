"""Normalize evidence and identify old-code skew regression candidates."""

import json

from pydantic import JsonValue

from skewguard.exec.types import (
    CandidateDivergence,
    Cell,
    CellResult,
    ComparisonIssue,
    ComparisonIssueKind,
    ComparisonPolicy,
    ComparisonResult,
    Outcome,
)

VOLATILE_MARKER = "<volatile>"


def mask_volatile_value(value: JsonValue) -> JsonValue:
    """Mask scalar identifiers/times while preserving nulls and boolean values."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return VOLATILE_MARKER
    return value


def normalize_value(
    value: JsonValue,
    *,
    volatile_fields: frozenset[str] = frozenset(),
) -> JsonValue:
    """Return a recursively copied JSON value with declared volatile leaves masked.

    """
    if isinstance(value, dict):
        normalized: dict[str, JsonValue] = {}

        for key, item in sorted(value.items()):
            if key in volatile_fields and not isinstance(item, (dict, list)):
                normalized[key] = mask_volatile_value(item)
            else:
                normalized[key] = normalize_value(item, volatile_fields=volatile_fields)

        return normalized

    if isinstance(value, list):
        return [
            normalize_value(item, volatile_fields=volatile_fields)
            for item in value
        ]

    return value

def normalize_outcome(
    outcome: Outcome,
    *,
    volatile_fields: frozenset[str] = frozenset(),
) -> Outcome:
    """Copy body evidence while retaining scenario IDs, statuses, and exceptions."""
    return outcome.model_copy(
        update={
            "body": normalize_value(outcome.body, volatile_fields=volatile_fields),
        }
    )


def baseline_is_usable(outcome: Outcome) -> bool:
    """Require successful ORM execution or a successful/redirecting HTTP response."""
    return outcome.status == "ok" or (
        outcome.status == "http_status"
        and outcome.http_status is not None
        and 200 <= outcome.http_status < 400
    )


def probe_input_is_unavailable(outcome: Outcome) -> bool:
    """Distinguish deterministic input-generation failures from application errors."""
    return (
        outcome.status == "exception"
        and outcome.exception_type is not None
        and outcome.exception_type.rsplit(".", 1)[-1] == "ProbeValueError"
    )


def validate_comparison_pair(baseline: Outcome, observed: Outcome, cell: Cell) -> None:
    """Reject mismatched checks and accidental comparisons across code versions."""
    if cell not in {Cell.A0D1, Cell.A0D1W}:
        raise ValueError("paired comparisons require A0D1 or A0D1w")
    if baseline.scenario_id != observed.scenario_id:
        raise ValueError("comparison requires matching scenario IDs")


def body_comparison_key(outcome: Outcome, policy: ComparisonPolicy) -> str:
    """Serialize normalized bodies without Python bool/int equality collapsing types."""
    normalized = normalize_outcome(outcome, volatile_fields=policy.volatile_fields)
    return json.dumps(
        normalized.body, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def compare_outcomes(
    baseline: Outcome,
    observed: Outcome,
    *,
    cell: Cell,
    policy: ComparisonPolicy | None = None,
) -> CandidateDivergence | None:
    """Classify a matching old-code check without declaring a verified verdict.

    Validate the pair. Ignore unusable baselines and ProbeValueError inputs;
    compare_matrix records those issues separately. Classify observed exceptions
    first (integrity_error before crash), then status changes, then opt-in
    normalized body changes. Retain raw outcomes and policy in any candidate.
    """
    raise NotImplementedError("outcome comparison is not implemented")


def compare_matrix(
    cells: list[CellResult],
    *,
    policies: dict[str, ComparisonPolicy] | None = None,
) -> ComparisonResult:
    """Compare old-code cells and retain coverage/health issues separately.

    New code is checked for execution health rather than response equivalence
    with the old release. ORM body comparisons are disabled unless requested.
    """
    by_cell = {result.cell: result for result in cells}
    if len(by_cell) != len(cells) or set(by_cell) != set(Cell):
        raise ValueError("comparison requires exactly one result for every cell")

    selected_policies = policies or {}
    baseline = {
        outcome.scenario_id: outcome for outcome in by_cell[Cell.A0D0].outcomes
    }
    candidates: list[CandidateDivergence] = []
    issues: list[ComparisonIssue] = []
    for cell in (Cell.A0D0, Cell.A1D1):
        if not by_cell[cell].outcomes:
            issues.append(
                ComparisonIssue(
                    cell=cell,
                    kind=ComparisonIssueKind.MISSING_CHECK,
                    message="The cell contains no executed checks.",
                )
            )
    for outcome in baseline.values():
        if not baseline_is_usable(outcome):
            kind = (
                ComparisonIssueKind.PROBE_INPUT_UNAVAILABLE
                if probe_input_is_unavailable(outcome)
                else ComparisonIssueKind.BASELINE_FAILED
            )
            issues.append(
                ComparisonIssue(
                    cell=Cell.A0D0,
                    kind=kind,
                    scenario_id=outcome.scenario_id,
                    message="The check has no successful baseline.",
                    outcome=outcome,
                )
            )

    for cell in (Cell.A0D1, Cell.A0D1W):
        observed = {outcome.scenario_id: outcome for outcome in by_cell[cell].outcomes}
        setup = {
            key
            for key in observed
            if cell == Cell.A0D1W and key.startswith("orm:") and key.endswith(":commit")
        }
        if cell == Cell.A0D1W and (
            not setup or any(observed[key].status != "ok" for key in setup)
        ):
            issues.append(
                ComparisonIssue(
                    cell=cell,
                    kind=ComparisonIssueKind.ROLLBACK_SETUP_FAILED,
                    message="Rollback checks require successful committed head writes.",
                )
            )
            continue
        for key, old in baseline.items():
            new = observed.get(key)
            if new is None:
                issues.append(
                    ComparisonIssue(
                        cell=cell,
                        kind=ComparisonIssueKind.MISSING_CHECK,
                        scenario_id=key,
                        message="The baseline check was not executed in this cell.",
                    )
                )
                continue
            if probe_input_is_unavailable(new):
                issues.append(
                    ComparisonIssue(
                        cell=cell,
                        kind=ComparisonIssueKind.PROBE_INPUT_UNAVAILABLE,
                        scenario_id=key,
                        message="Probe input could not be prepared.",
                        outcome=new,
                    )
                )
                continue
            if not baseline_is_usable(old):
                continue
            candidate = compare_outcomes(
                old, new, cell=cell, policy=selected_policies.get(key)
            )
            if candidate is not None:
                candidates.append(candidate)
        for key in sorted(observed.keys() - baseline.keys() - setup):
            issues.append(
                ComparisonIssue(
                    cell=cell,
                    kind=ComparisonIssueKind.UNEXPECTED_CHECK,
                    scenario_id=key,
                    message="The check has no matching old-code baseline.",
                    outcome=observed[key],
                )
            )

    for outcome in by_cell[Cell.A1D1].outcomes:
        if not baseline_is_usable(outcome):
            kind = (
                ComparisonIssueKind.PROBE_INPUT_UNAVAILABLE
                if probe_input_is_unavailable(outcome)
                else ComparisonIssueKind.NEW_CODE_FAILURE
            )
            issues.append(
                ComparisonIssue(
                    cell=Cell.A1D1,
                    kind=kind,
                    scenario_id=outcome.scenario_id,
                    message="The new-code check did not complete successfully.",
                    outcome=outcome,
                )
            )
    return ComparisonResult(candidates=candidates, issues=issues)
