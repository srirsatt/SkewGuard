"""Dispatch real shop requests through the selected release and database."""

import os

import pytest

from skewguard.config import RunConfig
from skewguard.exec.db import clone_database, managed_postgres
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import run_http_get, seed_database
from skewguard.exec.runner import run_migrations
from skewguard.exec.scenarios import Scenario
from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState


def request(**options: object) -> Scenario:
    return Scenario.model_validate(
        {
            "scenario_id": "http:catalog.products:list",
            "method": "GET",
            "path": "/products/",
            "kind": "read",
            **options,
        }
    )


@pytest.mark.parametrize(
    "options",
    [
        {"method": "POST", "kind": "write", "body": {"name": "Book"}},
        {"as_user": "buyer"},
        {"method": "HEAD"},
    ],
)
def test_executor_rejects_unsupported_options_before_dispatch(
    graph_inputs: tuple[RunState, RunResources],
    options: dict[str, object],
) -> None:
    state, resources = graph_inputs
    with pytest.raises(ValueError, match="anonymous GETs"):
        run_http_get(
            state.config,
            resources.base,
            resources.server,
            settings_module=state.settings_module,
            scenario=request(**options),
        )


@pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 for Docker",
)
def test_shop_get_uses_real_view_and_selected_release_across_a_rename(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    base, head = releases["base"], releases["rename"]
    scenario = request(compare_fields=["products"])
    settings = "shop.settings"
    with managed_postgres(config) as server:
        d0 = clone_database(config, server)
        run_migrations(config, base, d0, settings_module=settings)
        seed_database(config, base, d0, settings_module=settings, fixture="products")
        baseline = run_http_get(
            config, base, d0, settings_module=settings, scenario=scenario
        )
        assert baseline.scenario_id == scenario.scenario_id
        assert baseline.status == "http_status" and baseline.http_status == 200
        assert baseline.body == {
            "products": [
                {"sku": "PEN", "name": "Pen", "price": 5, "stock": 30},
                {"sku": "NOTEBOOK", "name": "Notebook", "price": 20, "stock": 12},
            ]
        }
        missing = run_http_get(
            config,
            base,
            d0,
            settings_module=settings,
            scenario=request(path="/missing/"),
        )
        assert missing.status == "http_status" and missing.http_status == 404

        d1 = clone_database(config, server, source=d0.dbname)
        run_migrations(config, head, d1, settings_module=settings)
        skew = run_http_get(
            config, base, d1, settings_module=settings, scenario=scenario
        )
        assert skew.status == "exception" and skew.exception_type == "ProgrammingError"
        assert "price" in (skew.exception_message or "")
        migrated = run_http_get(
            config, head, d1, settings_module=settings, scenario=scenario
        )
        assert migrated.http_status == 200
        assert migrated.body == {
            "products": [
                {"sku": "PEN", "name": "Pen", "amount": 5, "stock": 30},
                {"sku": "NOTEBOOK", "name": "Notebook", "amount": 20, "stock": 12},
            ]
        }
