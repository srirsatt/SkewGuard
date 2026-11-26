"""Check which HTTP proposals can supply evidence on the old release."""

import os

import pytest

from skewguard.config import RunConfig
from skewguard.exec import qualification
from skewguard.exec.db import clone_database, connect_database, managed_postgres
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import ProbeExecutionError, seed_database
from skewguard.exec.qualification import baseline_rejection_reason, qualify_scenario
from skewguard.exec.runner import run_migrations
from skewguard.exec.scenarios import Scenario
from skewguard.exec.types import Cell, ComparisonIssue, ComparisonIssueKind, Outcome
from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState

CREATE = Scenario(
    scenario_id="http:products:create",
    method="POST",
    path="/products/",
    kind="write",
    body={"sku": "MUG", "name": "Mug", "price": 12, "stock": 4},
    compare_fields=["price"],
)


@pytest.mark.parametrize("code", [200, 201, 302, 399])
def test_observable_http_baseline_qualifies(code: int) -> None:
    result = Outcome(
        scenario_id=CREATE.scenario_id,
        status="http_status",
        http_status=code,
        body={"price": None},
    )
    assert baseline_rejection_reason(CREATE, result) is None


@pytest.mark.parametrize("code", [199, 403, 404, 500])
def test_rejection_identifies_the_baseline_status(code: int) -> None:
    result = Outcome(
        scenario_id=CREATE.scenario_id, status="http_status", http_status=code
    )
    reason = baseline_rejection_reason(CREATE, result)
    assert reason and str(code) in reason


def test_http_body_only_needs_fields_selected_by_the_proposal() -> None:
    result = Outcome(
        scenario_id=CREATE.scenario_id, status="http_status", http_status=204
    )
    reason = baseline_rejection_reason(CREATE, result)
    assert reason and "price" in reason
    assert (
        baseline_rejection_reason(
            CREATE.model_copy(update={"compare_fields": []}), result
        )
        is None
    )


def test_rejection_preserves_exception_feedback() -> None:
    result = Outcome(
        scenario_id=CREATE.scenario_id,
        status="exception",
        exception_type="ValueError",
        exception_message="invalid SKU",
    )
    reason = baseline_rejection_reason(CREATE, result)
    assert reason and "ValueError" in reason and "invalid SKU" in reason


@pytest.mark.parametrize(
    "result",
    [
        Outcome(scenario_id=CREATE.scenario_id, status="ok"),
        Outcome(scenario_id="http:other", status="http_status", http_status=200),
    ],
)
def test_unrelated_evidence_cannot_qualify_a_proposal(result: Outcome) -> None:
    with pytest.raises(ValueError, match="matching HTTP"):
        baseline_rejection_reason(CREATE, result)


def test_unsupported_method_returns_feedback_before_execution(
    graph_inputs: tuple[RunState, RunResources],
) -> None:
    state, resources = graph_inputs
    proposal = CREATE.model_copy(update={"method": "PATCH"})
    result = qualify_scenario(
        state.config,
        resources.base,
        resources.server,
        settings_module=state.settings_module,
        scenario=proposal,
    )
    assert isinstance(result, ComparisonIssue) and result.outcome is None
    assert result.kind == ComparisonIssueKind.PROBE_INPUT_UNAVAILABLE
    assert result.cell == Cell.A0D0 and "PATCH" in result.message


@pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 for Docker",
)
def test_qualification_uses_and_releases_a_fresh_seeded_clone(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, releases = shop_releases
    base = releases["base"]
    settings = "shop.auth_settings"
    with managed_postgres(config) as server:
        old = clone_database(config, server)
        run_migrations(config, base, old, settings_module=settings)
        seed_database(config, base, old, settings_module=settings, fixture="products")

        def qualify(scenario: Scenario) -> Outcome | ComparisonIssue:
            return qualify_scenario(
                config, base, old, settings_module=settings, scenario=scenario
            )

        def databases() -> list[tuple[object, ...]]:
            with connect_database(config, server) as connection:
                return connection.execute(
                    "SELECT datname FROM pg_database ORDER BY datname"
                ).fetchall()

        before = databases()
        for _ in range(2):
            accepted = qualify(CREATE)
            assert isinstance(accepted, Outcome) and accepted.http_status == 201
            assert accepted.body == CREATE.body
        rejected = qualify(CREATE.model_copy(update={"compare_fields": ["total"]}))
        assert isinstance(rejected, ComparisonIssue)
        assert rejected.cell == Cell.A0D0 and "total" in rejected.message
        assert rejected.kind == ComparisonIssueKind.BASELINE_FAILED
        assert rejected.outcome and rejected.outcome.body == CREATE.body
        missing_user = qualify(CREATE.model_copy(update={"as_user": "missing"}))
        assert isinstance(missing_user, ComparisonIssue)
        assert missing_user.kind == ComparisonIssueKind.PROBE_INPUT_UNAVAILABLE
        assert (
            missing_user.outcome
            and missing_user.outcome.exception_type == "ProbeValueError"
        )

        def failed_worker(*args: object, **kwargs: object) -> Outcome:
            raise ProbeExecutionError("worker timed out")

        monkeypatch.setattr(qualification, "run_http_scenario", failed_worker)
        with pytest.raises(ProbeExecutionError, match="timed out"):
            qualify(CREATE)
        assert databases() == before
        with connect_database(config, old) as connection:
            assert connection.execute(
                "SELECT sku FROM catalog_product ORDER BY sku"
            ).fetchall() == [("NOTEBOOK",), ("PEN",)]
