"""Compare execution evidence without confusing missing coverage with regressions."""

import pytest
from pydantic import JsonValue, ValidationError

from skewguard.exec.diff import compare_matrix, compare_outcomes
from skewguard.exec.types import (
    CandidateDivergence,
    Cell,
    CellResult,
    ComparisonIssueKind,
    ComparisonPolicy,
    ComparisonResult,
    DivergenceCategory,
    Outcome,
)

READ = "orm:catalog.Product:read"
WRITE = "orm:catalog.Product:write"
COMMIT = "orm:catalog.Product:commit"


def success(body: JsonValue = None, *, scenario_id: str = READ) -> Outcome:
    return Outcome(scenario_id=scenario_id, status="ok", body=body)


def exception(
    *, scenario_id: str = READ, kind: str = "ProgrammingError", integrity: bool = False
) -> Outcome:
    return Outcome(
        scenario_id=scenario_id,
        status="exception",
        exception_type=kind,
        exception_message='column "price" does not exist',
        integrity_error=integrity,
    )


def healthy_matrix() -> list[CellResult]:
    baseline = [success([{"id": 1, "price": 5}]), success(scenario_id=WRITE)]
    return [
        CellResult(cell=Cell.A0D0, outcomes=baseline),
        CellResult(cell=Cell.A0D1, outcomes=baseline),
        CellResult(
            cell=Cell.A1D1,
            outcomes=[success([{"id": 1, "amount": 5}]), success(scenario_id=WRITE)],
        ),
        CellResult(
            cell=Cell.A0D1W,
            outcomes=[
                success({"created": True, "rolled_back": False}, scenario_id=COMMIT),
                success([{"id": 1, "price": 5}, {"id": 2, "price": 10}]),
                success(scenario_id=WRITE),
            ],
        ),
    ]


def replace_cell(
    cells: list[CellResult], cell: Cell, outcomes: list[Outcome]
) -> list[CellResult]:
    return [
        CellResult(cell=cell, outcomes=outcomes) if result.cell == cell else result
        for result in cells
    ]


@pytest.mark.parametrize(
    ("observed", "category"),
    [
        (exception(), DivergenceCategory.CRASH),
        (
            exception(kind="IntegrityError", integrity=True),
            DivergenceCategory.INTEGRITY_ERROR,
        ),
        (
            Outcome(scenario_id=READ, status="http_status", http_status=500),
            DivergenceCategory.STATUS_CHANGE,
        ),
    ],
)
def test_execution_failure_retains_raw_evidence(
    observed: Outcome, category: DivergenceCategory
) -> None:
    baseline = success({"id": 1})
    candidate = compare_outcomes(baseline, observed, cell=Cell.A0D1)

    assert candidate is not None
    assert candidate.category == category
    assert candidate.baseline == baseline
    assert candidate.observed == observed
    assert candidate.scenario_id == READ
    assert candidate.cell == Cell.A0D1
    assert (
        CandidateDivergence.model_validate_json(candidate.model_dump_json())
        == candidate
    )


@pytest.mark.parametrize("new_status", [201, 404, 500])
def test_http_status_changes_are_candidates(new_status: int) -> None:
    baseline = Outcome(scenario_id=READ, status="http_status", http_status=200)
    observed = Outcome(scenario_id=READ, status="http_status", http_status=new_status)

    candidate = compare_outcomes(baseline, observed, cell=Cell.A0D1W)

    assert candidate is not None
    assert candidate.category == DivergenceCategory.STATUS_CHANGE


@pytest.mark.parametrize(
    "baseline",
    [
        exception(),
        Outcome(scenario_id=READ, status="http_status", http_status=404),
        Outcome(scenario_id=READ, status="http_status", http_status=500),
    ],
)
def test_failed_baseline_cannot_establish_a_regression(baseline: Outcome) -> None:
    assert compare_outcomes(baseline, exception(), cell=Cell.A0D1) is None


@pytest.mark.parametrize("kind", ["ProbeValueError", "worker.ProbeValueError"])
def test_input_generation_failure_is_not_a_migration_candidate(kind: str) -> None:
    assert compare_outcomes(success(), exception(kind=kind), cell=Cell.A0D1) is None


def test_successful_probes_do_not_require_identical_bodies_by_default() -> None:
    assert (
        compare_outcomes(
            success([{"id": 1}]), success([{"id": 1}, {"id": 2}]), cell=Cell.A0D1W
        )
        is None
    )


def test_opt_in_body_comparison_masks_only_declared_noise() -> None:
    baseline = success({"id": 1, "price": 5})
    shifted = success({"price": 5, "id": 99})
    changed = success({"id": 99, "price": 6})
    policy = ComparisonPolicy(compare_body=True, volatile_fields=frozenset({"id"}))

    assert compare_outcomes(baseline, shifted, cell=Cell.A0D1, policy=policy) is None
    candidate = compare_outcomes(baseline, changed, cell=Cell.A0D1, policy=policy)

    assert candidate is not None
    assert candidate.category == DivergenceCategory.BODY_CHANGE
    assert candidate.policy == policy
    assert baseline.body == {"id": 1, "price": 5}
    assert candidate.observed.body == {"id": 99, "price": 6}


@pytest.mark.parametrize(("old", "new"), [(False, 0), (1, 1.0), (None, "")])
def test_body_comparison_preserves_json_value_types(
    old: JsonValue, new: JsonValue
) -> None:
    candidate = compare_outcomes(
        success(old),
        success(new),
        cell=Cell.A0D1,
        policy=ComparisonPolicy(compare_body=True),
    )

    assert candidate is not None
    assert candidate.category == DivergenceCategory.BODY_CHANGE


@pytest.mark.parametrize("cell", [Cell.A0D0, Cell.A1D1])
def test_pairing_rejects_cells_without_old_code_skew(cell: Cell) -> None:
    with pytest.raises(ValueError, match="A0D1"):
        compare_outcomes(success(), exception(), cell=cell)


def test_pairing_and_evidence_records_reject_mismatched_checks() -> None:
    with pytest.raises(ValueError, match="matching scenario IDs"):
        compare_outcomes(success(), exception(scenario_id=WRITE), cell=Cell.A0D1)
    with pytest.raises(ValidationError, match="matching scenario IDs"):
        CandidateDivergence(
            cell=Cell.A0D1,
            scenario_id=WRITE,
            category=DivergenceCategory.CRASH,
            baseline=success(),
            observed=exception(),
        )


def test_matrix_allows_new_code_shapes_and_expected_rollback_rows() -> None:
    comparison = compare_matrix(healthy_matrix())

    assert comparison.candidates == []
    assert comparison.issues == []
    assert (
        ComparisonResult.model_validate_json(comparison.model_dump_json()) == comparison
    )


def test_matrix_keeps_old_code_regressions_separate_from_new_code_health() -> None:
    cells = replace_cell(
        healthy_matrix(), Cell.A0D1, [exception(), success(scenario_id=WRITE)]
    )
    cells = replace_cell(cells, Cell.A1D1, [exception(), success(scenario_id=WRITE)])

    comparison = compare_matrix(cells)

    assert [(item.cell, item.category) for item in comparison.candidates] == [
        (Cell.A0D1, DivergenceCategory.CRASH)
    ]
    assert [(item.cell, item.kind) for item in comparison.issues] == [
        (Cell.A1D1, ComparisonIssueKind.NEW_CODE_FAILURE)
    ]


def test_matrix_uses_each_checks_explicit_comparison_policy() -> None:
    policy = ComparisonPolicy(compare_body=True, volatile_fields=frozenset({"id"}))
    comparison = compare_matrix(healthy_matrix(), policies={READ: policy})

    assert [(item.cell, item.category) for item in comparison.candidates] == [
        (Cell.A0D1W, DivergenceCategory.BODY_CHANGE)
    ]
    assert comparison.candidates[0].policy == policy


def test_failed_baseline_and_unavailable_input_remain_visible_as_issues() -> None:
    cells = replace_cell(
        healthy_matrix(), Cell.A0D0, [exception(), success(scenario_id=WRITE)]
    )
    cells = replace_cell(
        cells,
        Cell.A0D1,
        [exception(), exception(scenario_id=WRITE, kind="ProbeValueError")],
    )

    comparison = compare_matrix(cells)

    assert comparison.candidates == []
    assert [(item.cell, item.kind) for item in comparison.issues] == [
        (Cell.A0D0, ComparisonIssueKind.BASELINE_FAILED),
        (Cell.A0D1, ComparisonIssueKind.PROBE_INPUT_UNAVAILABLE),
    ]


def test_missing_and_unexpected_checks_are_coverage_issues() -> None:
    cells = replace_cell(
        healthy_matrix(), Cell.A0D1, [success(scenario_id="orm:catalog.Other:read")]
    )

    comparison = compare_matrix(cells)

    assert comparison.candidates == []
    assert [item.kind for item in comparison.issues] == [
        ComparisonIssueKind.MISSING_CHECK,
        ComparisonIssueKind.MISSING_CHECK,
        ComparisonIssueKind.UNEXPECTED_CHECK,
    ]


@pytest.mark.parametrize("cell", [Cell.A0D0, Cell.A1D1])
def test_empty_baseline_or_head_cell_is_visible(cell: Cell) -> None:
    comparison = compare_matrix(replace_cell(healthy_matrix(), cell, []))

    assert any(
        issue.cell == cell and issue.kind == ComparisonIssueKind.MISSING_CHECK
        for issue in comparison.issues
    )


@pytest.mark.parametrize("commit", [None, exception(scenario_id=COMMIT)])
def test_rollback_candidates_require_successful_head_write_setup(
    commit: Outcome | None,
) -> None:
    outcomes = [exception(), success(scenario_id=WRITE)]
    if commit is not None:
        outcomes.insert(0, commit)
    comparison = compare_matrix(replace_cell(healthy_matrix(), Cell.A0D1W, outcomes))

    assert comparison.candidates == []
    assert [item.kind for item in comparison.issues] == [
        ComparisonIssueKind.ROLLBACK_SETUP_FAILED
    ]


def test_matrix_requires_one_result_per_cell() -> None:
    cells = healthy_matrix()
    with pytest.raises(ValueError, match="exactly one"):
        compare_matrix(cells[:-1])
    with pytest.raises(ValueError, match="exactly one"):
        compare_matrix([*cells, cells[0]])
