"""Validate execution evidence before it reaches matrix comparison."""

import pytest
from pydantic import ValidationError

from skewguard.exec import types as execution_types


@pytest.mark.parametrize(
    "evidence",
    [
        {"status": "ok", "body": [{"name": "Alice"}]},
        {
            "status": "exception",
            "exception_type": "ProgrammingError",
            "exception_message": 'column "name" does not exist',
        },
        {"status": "http_status", "http_status": 200, "body": {"count": 1}},
        {"status": "http_status", "http_status": 500, "body": "Server Error"},
    ],
)
def test_outcome_preserves_evidence_through_json(
    evidence: dict[str, object],
) -> None:
    outcome = execution_types.Outcome.model_validate(
        {"scenario_id": "shop.Customer.read", **evidence}
    )

    restored = execution_types.Outcome.model_validate_json(outcome.model_dump_json())

    assert restored == outcome
    assert all(restored.model_dump()[key] == value for key, value in evidence.items())


def test_exception_message_may_be_empty() -> None:
    outcome = execution_types.Outcome.model_validate(
        {
            "scenario_id": "shop.Customer.read",
            "status": "exception",
            "exception_type": "RuntimeError",
            "exception_message": "",
        }
    )

    assert outcome.exception_message == ""


@pytest.mark.parametrize(
    "evidence",
    [
        {"status": "exception"},
        {"status": "exception", "exception_type": "ProgrammingError"},
        {"status": "exception", "exception_message": "missing column"},
        {
            "status": "exception",
            "exception_type": "",
            "exception_message": "missing column",
        },
        {
            "status": "exception",
            "exception_type": "ProgrammingError",
            "exception_message": "missing column",
            "http_status": 500,
        },
        {"status": "ok", "exception_type": "RuntimeError"},
        {"status": "ok", "exception_message": ""},
        {"status": "ok", "http_status": 200},
        {"status": "ok", "integrity_error": True},
        {"status": "http_status"},
        {
            "status": "http_status",
            "http_status": 500,
            "exception_type": "RuntimeError",
        },
        {"status": "http_status", "http_status": 500, "exception_message": ""},
        {"status": "http_status", "http_status": 500, "integrity_error": True},
    ],
)
def test_outcome_rejects_inconsistent_evidence(evidence: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        execution_types.Outcome.model_validate(
            {"scenario_id": "shop.Customer.read", **evidence}
        )


@pytest.mark.parametrize("http_status", [99, 600, "200", 200.0, True])
def test_http_status_requires_an_integer_in_range(http_status: object) -> None:
    with pytest.raises(ValidationError):
        execution_types.Outcome.model_validate(
            {
                "scenario_id": "shop.Customer.read",
                "status": "http_status",
                "http_status": http_status,
            }
        )


def test_body_must_be_json_compatible() -> None:
    with pytest.raises(ValidationError):
        execution_types.Outcome.model_validate(
            {"scenario_id": "shop.Customer.read", "status": "ok", "body": object()}
        )


def test_integrity_error_is_recorded_as_exception_evidence() -> None:
    outcome = execution_types.Outcome.model_validate(
        {
            "scenario_id": "shop.Customer.create",
            "status": "exception",
            "exception_type": "IntegrityError",
            "exception_message": "duplicate key value violates unique constraint",
            "integrity_error": True,
        }
    )

    assert outcome.integrity_error is True
    assert outcome.status == "exception"


def test_cell_result_preserves_nested_outcomes_and_cell_label() -> None:
    result = execution_types.CellResult.model_validate(
        {
            "cell": "A0D1w",
            "outcomes": [
                {"scenario_id": "shop.Customer.read", "status": "ok"},
                {"scenario_id": "shop.Order.read", "status": "ok"},
            ],
        }
    )

    assert result.cell is execution_types.Cell.A0D1W
    assert result.model_dump(mode="json")["cell"] == "A0D1w"
    assert result.outcomes[0].scenario_id == "shop.Customer.read"
    assert execution_types.CellResult.model_validate_json(result.model_dump_json()) == (
        result
    )


def test_cell_result_rejects_duplicate_scenario_ids() -> None:
    read = execution_types.Outcome(scenario_id="shop.Customer.read", status="ok")
    write = execution_types.Outcome(scenario_id="shop.Customer.create", status="ok")
    failed_read = execution_types.Outcome.model_validate(
        {
            "scenario_id": "shop.Customer.read",
            "status": "exception",
            "exception_type": "ProgrammingError",
            "exception_message": "missing column",
        }
    )

    with pytest.raises(ValidationError, match="duplicate scenario_id"):
        execution_types.CellResult(
            cell=execution_types.Cell.A0D0, outcomes=[read, write, failed_read]
        )


def test_cell_result_rejects_invalid_nested_outcomes() -> None:
    with pytest.raises(ValidationError) as error:
        execution_types.CellResult.model_validate(
            {
                "cell": "A0D1",
                "outcomes": [
                    {"scenario_id": "shop.Customer.read", "status": "exception"}
                ],
            }
        )

    assert error.value.errors()[0]["loc"] == ("outcomes", 0)


def test_scenario_id_can_be_reused_across_different_cells() -> None:
    outcome = execution_types.Outcome(scenario_id="shop.Customer.read", status="ok")

    baseline = execution_types.CellResult(
        cell=execution_types.Cell.A0D0, outcomes=[outcome]
    )
    migrated = execution_types.CellResult(
        cell=execution_types.Cell.A0D1, outcomes=[outcome]
    )

    assert baseline.outcomes[0].scenario_id == migrated.outcomes[0].scenario_id
