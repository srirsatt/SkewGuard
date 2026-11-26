"""Normalize evidence and identify old-code skew regression candidates."""

from skewguard.exec.types import DivergenceCategory
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


def select_response_fields(
    body: JsonValue, fields: tuple[str, ...]
) -> dict[str, JsonValue]:
    """Return present top-level fields, including nulls; non-objects yield {}."""
    
    # select the right key for check
    if not isinstance(body, dict):
        return {}

    return {key: body[key] for key in fields if key in body} 
    # if its in both then add it to our return {}


def missing_baseline_fields(
    outcome: Outcome, policy: ComparisonPolicy | None
) -> tuple[str, ...]:
    """A selected key must be observable in the old release's baseline response."""
    if policy is None or not policy.compare_fields:
        return ()
    body = outcome.body
    if not isinstance(body, dict):
        return policy.compare_fields
    return tuple(key for key in policy.compare_fields if key not in body)


def body_comparison_key(outcome: Outcome, policy: ComparisonPolicy) -> str:
    """Serialize normalized bodies without Python bool/int equality collapsing types."""
    body = outcome.body
    if policy.compare_fields:
        body = select_response_fields(body, policy.compare_fields)
    normalized = normalize_value(body, volatile_fields=policy.volatile_fields)
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def compare_outcomes(
    baseline: Outcome,
    observed: Outcome,
    *,
    cell: Cell,
    policy: ComparisonPolicy | None = None,
) -> CandidateDivergence | None:
    """Classify a matching old-code check without declaring a verified verdict.

    """

    validate_comparison_pair(baseline, observed, cell)
    # this function is to check divergence from different cells

    selected = policy if policy is not None else ComparisonPolicy()

    # make sure its valid evidence via helpers

    if (
        not baseline_is_usable(baseline)
        or missing_baseline_fields(baseline, selected)
        or probe_input_is_unavailable(observed)
    ):
        return None

    # classify exception type
    if observed.status == "exception":
        category = (
            DivergenceCategory.INTEGRITY_ERROR
            if observed.integrity_error else DivergenceCategory.CRASH
        )
    elif (baseline.status, baseline.http_status) != (
        observed.status, observed.http_status
    ):
        category = DivergenceCategory.STATUS_CHANGE
    elif selected.compare_body and body_comparison_key(baseline, selected) != body_comparison_key(observed, selected):
        category = DivergenceCategory.BODY_CHANGE
    else:
        return None

    # once we've classified + verified evidence validity, return as a CandidateDivergence type

    return CandidateDivergence(
        cell=cell,
        scenario_id=baseline.scenario_id,
        category=category,
        baseline=baseline,
        observed=observed,
        policy=selected,
    )


def rollback_references(
    old: CellResult,
    rollback: CellResult,
    baseline: dict[str, Outcome],
    policies: dict[str, ComparisonPolicy],
    issues: list[ComparisonIssue],
) -> dict[str, Outcome] | None:
    """Pair old reads after head writes with old reads after equivalent base writes."""
    writes = [s.scenario_id for s in old.qualified_scenarios if s.kind == "write"]
    batches = [("head", rollback.rollback_writes)]
    if old.rollback_controls:
        batches.append(("base", old.rollback_writes))
    for release, batch in batches:
        if [item.scenario_id for item in batch] != writes:
            issues.append(
                ComparisonIssue(
                    cell=Cell.A0D1W,
                    kind=ComparisonIssueKind.ROLLBACK_SETUP_FAILED,
                    message=f"Unexpected HTTP write sequence on the {release} release.",
                )
            )
            return None
        for item in batch:
            if (
                not baseline_is_usable(item)
                or item.http_status != baseline[item.scenario_id].http_status
            ):
                issues.append(
                    ComparisonIssue(
                        cell=Cell.A0D1W,
                        kind=ComparisonIssueKind.ROLLBACK_SETUP_FAILED,
                        outcome=item,
                        message=(
                            f"The {release} write {item.scenario_id} "
                            "did not reproduce its baseline status."
                        ),
                    )
                )
                return None

    references = baseline.copy()
    controls = {item.scenario_id: item for item in old.rollback_controls}
    for scenario in old.qualified_scenarios:
        key = scenario.scenario_id
        del references[key]
        if scenario.kind == "write":
            continue
        control = controls.get(key)
        if (
            control
            and baseline_is_usable(control)
            and not missing_baseline_fields(control, policies.get(key))
        ):
            references[key] = control
        elif not any(
            issue.cell == Cell.A0D1W and issue.scenario_id == key for issue in issues
        ):
            issues.append(
                ComparisonIssue(
                    cell=Cell.A0D1W,
                    kind=ComparisonIssueKind.ROLLBACK_SETUP_FAILED,
                    scenario_id=key,
                    outcome=control,
                    message="The old-release read lacks a usable post-write baseline.",
                )
            )
    return references


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

    old_cell = by_cell[Cell.A0D0]
    selected_policies = {
        item.scenario_id: item.comparison_policy()
        for item in old_cell.qualified_scenarios
    }
    selected_policies.update(policies or {})
    baseline = {
        outcome.scenario_id: outcome for outcome in by_cell[Cell.A0D0].outcomes
    }
    candidates: list[CandidateDivergence] = []
    issues = [issue for result in cells for issue in result.execution_issues]
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

        elif missing := missing_baseline_fields(
            outcome, selected_policies.get(outcome.scenario_id)
        ):
            issues.append(
                ComparisonIssue(
                    cell=Cell.A0D0,
                    kind=ComparisonIssueKind.BASELINE_FAILED,
                    scenario_id=outcome.scenario_id,
                    message="Baseline response lacks keys: " + ", ".join(missing),
                    outcome=outcome,
                )
            )

    for cell in (Cell.A0D1, Cell.A0D1W):
        references = baseline
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
        if cell == Cell.A0D1W:
            if any(
                issue.cell == cell and issue.scenario_id is None for issue in issues
            ):
                continue
            if old_cell.qualified_scenarios:
                paired = rollback_references(
                    old_cell, by_cell[cell], baseline, selected_policies, issues
                )
                if paired is None:
                    continue
                references = paired
        for key, old in references.items():
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
