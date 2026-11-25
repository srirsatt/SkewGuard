"""Response selection controls comparison while preserving raw HTTP evidence."""

import pytest
from pydantic import JsonValue

from skewguard.exec.diff import compare_matrix, compare_outcomes, select_response_fields
from skewguard.exec.scenarios import Scenario
from skewguard.exec.types import (
    Cell,
    CellResult,
    ComparisonIssueKind,
    ComparisonPolicy,
    DivergenceCategory,
    Outcome,
)
from skewguard.exec.verify import same_divergence

CREATE = Scenario(
    scenario_id="http:catalog.products:create",
    method="POST",
    path="/products/",
    kind="write",
    compare_fields=["price"],
)


def response(body: JsonValue, code: int = 201) -> Outcome:
    return Outcome(
        scenario_id=CREATE.scenario_id,
        status="http_status",
        http_status=code,
        body=body,
    )


def test_selects_all_present_top_level_keys_including_nested_values() -> None:
    body: JsonValue = {"price": 12, "details": {"currency": "USD"}, "id": 71}
    assert select_response_fields(body, ("price", "details", "currency")) == {
        "price": 12,
        "details": {"currency": "USD"},
    }


def test_scenario_without_fields_keeps_status_only_comparison() -> None:
    scenario = CREATE.model_copy(update={"compare_fields": []})
    assert scenario.comparison_policy() == ComparisonPolicy()
    assert (
        compare_outcomes(
            response({"price": 12}),
            response({"price": 15}),
            cell=Cell.A0D1,
            policy=scenario.comparison_policy(),
        )
        is None
    )


def test_selection_and_replay_ignore_unselected_values_and_keep_full_evidence() -> None:
    base = response({"price": 12, "id": 1, "meta": {"price": 100}})
    policy = CREATE.comparison_policy()
    assert ComparisonPolicy.model_validate_json(policy.model_dump_json()) == policy
    assert (
        compare_outcomes(
            base, response({"price": 12, "id": 8}), cell=Cell.A0D1, policy=policy
        )
        is None
    )
    observed = response({"price": 15, "id": 8})
    initial = compare_outcomes(base, observed, cell=Cell.A0D1, policy=policy)
    assert initial is not None and initial.category == DivergenceCategory.BODY_CHANGE
    assert initial.baseline == base and initial.observed == observed
    for replay_price, expected_match in [(15, True), (19, False)]:
        replay = compare_outcomes(
            response({"price": 12, "id": 4}),
            response({"price": replay_price, "id": 21}),
            cell=Cell.A0D1,
            policy=policy,
        )
        assert replay is not None
        assert same_divergence(initial, replay) is expected_match


@pytest.mark.parametrize("observed", [{}, {"price": False}, ["price"], None])
def test_missing_or_changed_field_differs_from_a_present_null(
    observed: JsonValue,
) -> None:
    result = compare_outcomes(
        response({"price": None}),
        response(observed),
        cell=Cell.A0D1,
        policy=CREATE.comparison_policy(),
    )
    assert result is not None and result.category == DivergenceCategory.BODY_CHANGE


def test_status_change_takes_priority_over_field_comparison() -> None:
    result = compare_outcomes(
        response({"price": 12}),
        response({"error": "bad"}, 400),
        cell=Cell.A0D1,
        policy=CREATE.comparison_policy(),
    )
    assert result is not None and result.category == DivergenceCategory.STATUS_CHANGE


@pytest.mark.parametrize("body", [{"nested": {"price": 12}}, [], None])
def test_unobservable_field_in_baseline_is_a_coverage_issue(body: JsonValue) -> None:
    cells = [CellResult(cell=cell, outcomes=[response(body)]) for cell in Cell]
    cells[-1] = CellResult(
        cell=Cell.A0D1W,
        outcomes=[
            Outcome(scenario_id="orm:catalog.Product:commit", status="ok"),
            response(body),
        ],
    )
    result = compare_matrix(
        cells, policies={CREATE.scenario_id: CREATE.comparison_policy()}
    )
    assert result.candidates == []
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.cell == Cell.A0D0 and issue.kind == ComparisonIssueKind.BASELINE_FAILED
    assert issue.scenario_id == CREATE.scenario_id and "price" in issue.message
