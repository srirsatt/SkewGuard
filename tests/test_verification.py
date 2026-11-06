"""Require two matching fresh-state replays and retain flaky/incomplete evidence."""

import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import SecretStr, ValidationError

import skewguard.exec.verify as verification
from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection, DatabaseSetupError
from skewguard.exec.diff import compare_matrix
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import ProbeExecutionError
from skewguard.exec.types import (
    CandidateDivergence,
    Cell,
    CellResult,
    ComparisonIssue,
    ComparisonIssueKind,
    ComparisonPolicy,
    ComparisonResult,
    DivergenceCategory,
    Outcome,
    ReproductionAttempt,
    VerificationResult,
)
from skewguard.exec.verify import classify_reproductions, same_divergence, verify_matrix

READ = "orm:catalog.Product:read"
COMMIT = "orm:catalog.Product:commit"


def matrix(*, broken: bool = True) -> list[CellResult]:
    good = Outcome(scenario_id=READ, status="ok", body=[{"id": 1, "price": 5}])
    bad = Outcome(
        scenario_id=READ,
        status="exception",
        exception_type="ProgrammingError",
        exception_message='column "price" does not exist',
    )
    return [
        CellResult(cell=Cell.A0D0, outcomes=[good]),
        CellResult(cell=Cell.A0D1, outcomes=[bad if broken else good]),
        CellResult(cell=Cell.A1D1, outcomes=[good]),
        CellResult(
            cell=Cell.A0D1W,
            outcomes=[
                Outcome(
                    scenario_id=COMMIT,
                    status="ok",
                    body={"created": True, "rolled_back": False},
                ),
                good,
            ],
        ),
    ]


def replay(
    number: int,
    *,
    cells: list[CellResult] | None = None,
    comparison: ComparisonResult | None = None,
) -> ReproductionAttempt:
    selected = cells if cells is not None else matrix()
    return ReproductionAttempt(
        number=number,
        cells=selected,
        comparison=comparison if comparison is not None else compare_matrix(selected),
    )


def test_two_matching_replays_confirm_and_preserve_initial_evidence() -> None:
    initial = compare_matrix(matrix())

    decisions = classify_reproductions(initial, [replay(1), replay(2)])

    assert [item.status for item in decisions] == ["confirmed"]
    assert decisions[0].candidate == initial.candidates[0]
    assert decisions[0].reason


@pytest.mark.parametrize("successful_attempt", [1, 2])
def test_a_disappearing_failure_is_flaky(successful_attempt: int) -> None:
    attempts = [
        replay(number, cells=matrix(broken=number != successful_attempt))
        for number in (1, 2)
    ]

    decisions = classify_reproductions(compare_matrix(matrix()), attempts)

    assert [item.status for item in decisions] == ["flaky"]


@pytest.mark.parametrize("field", ["exception_type", "exception_message"])
def test_a_different_exception_is_not_confirmation(field: str) -> None:
    initial = compare_matrix(matrix())
    candidate = initial.candidates[0]
    changed = candidate.model_copy(
        update={"observed": candidate.observed.model_copy(update={field: "different"})}
    )
    attempts = [
        replay(1),
        replay(2, comparison=ComparisonResult(candidates=[changed])),
    ]

    assert not same_divergence(candidate, changed)
    assert classify_reproductions(initial, attempts)[0].status == "flaky"


def test_body_reproduction_uses_the_same_explicit_normalization_policy() -> None:
    policy = ComparisonPolicy(compare_body=True, volatile_fields=frozenset({"id"}))
    candidate = CandidateDivergence(
        cell=Cell.A0D1,
        scenario_id=READ,
        category=DivergenceCategory.BODY_CHANGE,
        baseline=Outcome(scenario_id=READ, status="ok", body={"id": 1, "price": 5}),
        observed=Outcome(scenario_id=READ, status="ok", body={"id": 1, "price": 6}),
        policy=policy,
    )
    shifted = candidate.model_copy(
        update={
            "baseline": candidate.baseline.model_copy(
                update={"body": {"id": 99, "price": 5}}
            ),
            "observed": candidate.observed.model_copy(
                update={"body": {"id": 99, "price": 6}}
            ),
        }
    )
    different = shifted.model_copy(
        update={
            "observed": shifted.observed.model_copy(
                update={"body": {"id": 99, "price": 7}}
            )
        }
    )

    assert same_divergence(candidate, shifted)
    assert not same_divergence(candidate, different)
    assert not same_divergence(
        candidate, candidate.model_copy(update={"policy": ComparisonPolicy()})
    )


@pytest.mark.parametrize(
    ("cell", "kind"),
    [
        (Cell.A0D0, ComparisonIssueKind.BASELINE_FAILED),
        (Cell.A0D1, ComparisonIssueKind.MISSING_CHECK),
        (Cell.A0D1, ComparisonIssueKind.PROBE_INPUT_UNAVAILABLE),
    ],
)
def test_relevant_coverage_gaps_are_inconclusive(
    cell: Cell, kind: ComparisonIssueKind
) -> None:
    initial = compare_matrix(matrix())
    comparison = ComparisonResult(
        issues=[
            ComparisonIssue(
                cell=cell, kind=kind, scenario_id=READ, message="unavailable"
            )
        ]
    )

    decisions = classify_reproductions(
        initial, [replay(1), replay(2, comparison=comparison)]
    )

    assert decisions[0].status == "inconclusive"


def test_rollback_setup_gap_blocks_only_the_rollback_candidate() -> None:
    deploy = compare_matrix(matrix()).candidates[0]
    rollback = deploy.model_copy(update={"cell": Cell.A0D1W})
    initial = ComparisonResult(candidates=[deploy, rollback])
    repeated = ComparisonResult(
        candidates=[deploy],
        issues=[
            ComparisonIssue(
                cell=Cell.A0D1W,
                kind=ComparisonIssueKind.ROLLBACK_SETUP_FAILED,
                message="head writes failed",
            )
        ],
    )

    decisions = classify_reproductions(
        initial, [replay(1, comparison=repeated), replay(2, comparison=repeated)]
    )

    assert [item.status for item in decisions] == ["confirmed", "inconclusive"]


def test_each_original_candidate_is_classified_without_promoting_new_candidates() -> (
    None
):
    original = compare_matrix(matrix()).candidates[0]
    disappearing = original.model_copy(update={"cell": Cell.A0D1W})
    new = original.model_copy(
        update={
            "scenario_id": "new-check",
            "baseline": original.baseline.model_copy(
                update={"scenario_id": "new-check"}
            ),
            "observed": original.observed.model_copy(
                update={"scenario_id": "new-check"}
            ),
        }
    )
    initial = ComparisonResult(candidates=[original, disappearing])
    repeated = ComparisonResult(candidates=[original, new])

    decisions = classify_reproductions(
        initial, [replay(1, comparison=repeated), replay(2, comparison=repeated)]
    )

    assert [item.candidate for item in decisions] == [original, disappearing]
    assert [item.status for item in decisions] == ["confirmed", "flaky"]


def test_failed_replay_is_inconclusive_even_when_the_other_replay_matches() -> None:
    decisions = classify_reproductions(
        compare_matrix(matrix()),
        [ReproductionAttempt(number=1, error="database unavailable"), replay(2)],
    )

    assert decisions[0].status == "inconclusive"


@pytest.mark.parametrize("numbers", [[], [1], [1, 1], [2, 2], [1, 2, 2]])
def test_classification_requires_two_distinct_attempt_numbers(
    numbers: list[int],
) -> None:
    with pytest.raises(ValueError, match="two"):
        classify_reproductions(compare_matrix(matrix()), [replay(n) for n in numbers])


def test_replay_records_reject_missing_or_conflicting_evidence() -> None:
    with pytest.raises(ValidationError, match="requires cells and comparison"):
        ReproductionAttempt(number=1)
    with pytest.raises(ValidationError, match="cannot also contain"):
        ReproductionAttempt(number=1, error="failed", cells=matrix())
    with pytest.raises(ValidationError, match="exactly one"):
        ReproductionAttempt(number=1, cells=[], comparison=ComparisonResult())


@pytest.fixture
def verification_inputs(
    tmp_path: Path,
) -> tuple[RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection]:
    config = RunConfig(repo_path=tmp_path, base_ref="base", head_ref="head")
    base = PythonEnvironment(
        checkout=tmp_path / "base",
        venv=tmp_path / "base/.skewguard-venv",
        python=tmp_path / "base/.skewguard-venv/bin/python",
    )
    head = base.model_copy(update={"checkout": tmp_path / "head"})
    server = DatabaseConnection(
        host="localhost",
        port=5432,
        username="test",
        password=SecretStr("replay-secret"),
        dbname="postgres",
    )
    return config, base, head, server


def test_runner_replays_twice_with_the_same_inputs_and_retains_all_cells(
    verification_inputs: tuple[
        RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(side_effect=[matrix(), matrix(), matrix()])
    monkeypatch.setattr(verification, "run_matrix", run)
    policy = ComparisonPolicy()

    result = verify_matrix(
        *verification_inputs,
        settings_module="shop.settings",
        seed_fixture="products",
        policies={READ: policy},
    )

    assert run.call_count == 3
    assert all(call == run.call_args_list[0] for call in run.call_args_list)
    assert run.call_args_list[0].args == verification_inputs
    assert run.call_args_list[0].kwargs == {
        "settings_module": "shop.settings",
        "seed_fixture": "products",
    }
    assert [attempt.number for attempt in result.attempts] == [1, 2]
    assert all(attempt.cells is not None for attempt in result.attempts)
    assert result.decisions[0].status == "confirmed"
    assert result.decisions[0].candidate.policy == policy
    assert VerificationResult.model_validate_json(result.model_dump_json()) == result


def test_runner_skips_replays_without_candidates(
    verification_inputs: tuple[
        RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=matrix(broken=False))
    monkeypatch.setattr(verification, "run_matrix", run)

    result = verify_matrix(*verification_inputs, settings_module="shop.settings")

    assert run.call_count == 1
    assert result.attempts == []
    assert result.decisions == []


def test_runner_records_both_attempts_redacts_errors_and_logs_inconclusive(
    verification_inputs: tuple[
        RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection
    ],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = Mock(
        side_effect=[
            matrix(),
            ProbeExecutionError("replay-secret unavailable"),
            matrix(),
        ]
    )
    monkeypatch.setattr(verification, "run_matrix", run)

    with caplog.at_level(logging.WARNING, logger="skewguard.exec.verify"):
        result = verify_matrix(*verification_inputs, settings_module="shop.settings")

    assert run.call_count == 3
    assert result.attempts[0].error == "ProbeExecutionError: [redacted] unavailable"
    assert result.decisions[0].status == "inconclusive"
    assert "inconclusive" in caplog.text
    assert "replay-secret" not in caplog.text
    assert "replay-secret" not in result.model_dump_json()


def test_runner_logs_and_retains_flaky_candidates(
    verification_inputs: tuple[
        RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection
    ],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        verification,
        "run_matrix",
        Mock(side_effect=[matrix(), matrix(broken=False), matrix()]),
    )

    with caplog.at_level(logging.WARNING, logger="skewguard.exec.verify"):
        result = verify_matrix(*verification_inputs, settings_module="shop.settings")

    assert result.decisions[0].status == "flaky"
    assert result.initial_comparison.candidates
    assert "flaky" in caplog.text


def test_initial_setup_failure_propagates_without_a_verdict(
    verification_inputs: tuple[
        RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(side_effect=DatabaseSetupError("cannot prepare baseline"))
    monkeypatch.setattr(verification, "run_matrix", run)

    with pytest.raises(DatabaseSetupError, match="baseline"):
        verify_matrix(*verification_inputs, settings_module="shop.settings")
    assert run.call_count == 1


def test_unexpected_programming_errors_propagate(
    verification_inputs: tuple[
        RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        verification, "run_matrix", Mock(side_effect=[matrix(), ValueError("bug")])
    )

    with pytest.raises(ValueError, match="bug"):
        verify_matrix(*verification_inputs, settings_module="shop.settings")
