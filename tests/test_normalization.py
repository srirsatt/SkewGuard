"""Ensure normalization removes declared noise without erasing failure evidence."""

import json
from copy import deepcopy

import pytest
from pydantic import JsonValue

from skewguard.exec.diff import (
    VOLATILE_MARKER,
    normalize_outcome,
    normalize_value,
)
from skewguard.exec.types import Outcome


def test_declared_volatile_fields_are_masked_inside_nested_rows() -> None:
    body: JsonValue = {
        "products": [
            {
                "id": 91,
                "name": "Notebook",
                "price": 20,
                "created_at": "2026-11-05T09:00:00Z",
                "metadata": {"uuid": "d1ea1340-740b-480a-a12e-227e97a61c71"},
            }
        ]
    }

    result = normalize_value(
        body, volatile_fields=frozenset({"id", "uuid", "created_at"})
    )

    assert result == {
        "products": [
            {
                "id": VOLATILE_MARKER,
                "name": "Notebook",
                "price": 20,
                "created_at": VOLATILE_MARKER,
                "metadata": {"uuid": VOLATILE_MARKER},
            }
        ]
    }


def test_default_policy_preserves_ids_times_and_uuid_shaped_business_values() -> None:
    body: JsonValue = {
        "id": 91,
        "created_at": "2026-11-05T09:00:00Z",
        "sku": "d1ea1340-740b-480a-a12e-227e97a61c71",
        "price": 20,
        "ratio": 1.0,
        "enabled": True,
    }

    normalized = normalize_value(body)

    assert normalized == body
    assert isinstance(normalized, dict)
    assert type(normalized["ratio"]) is float
    assert type(normalized["enabled"]) is bool


def test_normalization_preserves_order_row_counts_and_business_changes() -> None:
    policy = frozenset({"id"})
    baseline: JsonValue = [{"id": 1, "price": 5}, {"id": 2, "price": 20}]
    shifted_ids: JsonValue = [{"id": 11, "price": 5}, {"id": 12, "price": 20}]
    reordered: JsonValue = [{"id": 12, "price": 20}, {"id": 11, "price": 5}]
    changed: JsonValue = [{"id": 11, "price": 6}, {"id": 12, "price": 20}]
    extra_row: JsonValue = [
        {"id": 11, "price": 5},
        {"id": 12, "price": 20},
        {"id": 13, "price": 1},
    ]
    normalized = normalize_value(baseline, volatile_fields=policy)

    assert normalized == normalize_value(shifted_ids, volatile_fields=policy)
    assert normalized != normalize_value(reordered, volatile_fields=policy)
    assert normalized != normalize_value(changed, volatile_fields=policy)
    assert normalized != normalize_value(extra_row, volatile_fields=policy)


def test_missing_null_boolean_and_container_changes_remain_observable() -> None:
    policy = frozenset({"id"})
    normalized = normalize_value({"id": 1}, volatile_fields=policy)

    assert normalized != normalize_value({}, volatile_fields=policy)
    assert normalized != normalize_value({"id": None}, volatile_fields=policy)
    assert normalized != normalize_value({"id": True}, volatile_fields=policy)
    assert normalized != normalize_value({"id": {"value": 1}}, volatile_fields=policy)
    assert normalized != normalize_value({"id": [1]}, volatile_fields=policy)


def test_normalization_is_repeatable_and_does_not_mutate_the_original() -> None:
    policy = frozenset({"id"})
    body: JsonValue = {"rows": [{"price": 5, "id": 1}], "count": 1}
    original = deepcopy(body)
    reordered_keys: JsonValue = {"count": 1, "rows": [{"id": 1, "price": 5}]}

    normalized = normalize_value(body, volatile_fields=policy)

    assert body == original
    assert normalized is not body
    assert normalize_value(normalized, volatile_fields=policy) == normalized
    assert json.dumps(normalized) == json.dumps(
        normalize_value(reordered_keys, volatile_fields=policy)
    )
    assert isinstance(normalized, dict)
    assert normalized["rows"] is not body["rows"]


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome(
            scenario_id="orm:catalog.Product:write",
            status="exception",
            exception_type="IntegrityError",
            exception_message='null value in column "category" (row id=91)',
            integrity_error=True,
            body={"id": 91},
        ),
        Outcome(
            scenario_id="http:products:read",
            status="http_status",
            http_status=500,
            body={"id": 91},
        ),
    ],
)
def test_outcome_normalization_retains_status_and_exception_evidence(
    outcome: Outcome,
) -> None:
    original = deepcopy(outcome.model_dump())

    normalized = normalize_outcome(outcome, volatile_fields=frozenset({"id"}))

    assert normalized is not outcome
    assert normalized.body == {"id": VOLATILE_MARKER}
    assert normalized.model_dump(exclude={"body"}) == outcome.model_dump(
        exclude={"body"}
    )
    assert outcome.model_dump() == original
