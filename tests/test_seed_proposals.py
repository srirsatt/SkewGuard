"""Check proposed rows through the old release's ORM and real shop view."""

import os

import pytest

from skewguard.agents.seed import SeedProposal
from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection, managed_postgres, temporary_clone
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.matrix import prepare_old_snapshot
from skewguard.exec.probes import insert_seed_proposal, run_http_get
from skewguard.exec.scenarios import Scenario

pytestmark = pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 to run tests requiring Docker",
)
SETTINGS = "shop.settings"
MUG = SeedProposal(
    model="catalog.Product",
    fields={"sku": "MUG-01", "name": "Mug", "price": 5, "stock": 0},
)


def visible_skus(
    config: RunConfig, base: PythonEnvironment, database: DatabaseConnection,
    path: str = "/products/",
) -> set[str]:
    scenario = Scenario(
        scenario_id="http:list-products", method="GET", kind="read", path=path,
    )
    outcome = run_http_get(
        config, base, database, settings_module=SETTINGS, scenario=scenario,
    )
    assert outcome.http_status == 200, outcome
    assert isinstance(outcome.body, dict)
    rows = outcome.body["products"]
    assert isinstance(rows, list)
    skus: set[str] = set()
    for row in rows:
        assert isinstance(row, dict) and isinstance(row["sku"], str)
        skus.add(row["sku"])
    return skus


def test_proposed_row_is_visible_only_if_it_matches_the_get(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    base = releases["base"]
    filtered = "/products/?out_of_stock=1"
    with managed_postgres(config) as server:
        old = prepare_old_snapshot(
            config, base, server, settings_module=SETTINGS, seed_fixture="products",
        )
        original = visible_skus(config, base, old)
        assert original == {"PEN", "NOTEBOOK"}
        for stock in (0, 1):
            with temporary_clone(config, old) as database:
                proposal = SeedProposal(
                    model=MUG.model, fields={**MUG.fields, "stock": stock}
                )
                inserted = insert_seed_proposal(
                    config, base, database, settings_module=SETTINGS, proposal=proposal,
                )
                assert inserted.status == "ok", inserted
                assert visible_skus(config, base, database) == original | {"MUG-01"}
                expected = {"MUG-01"} if stock == 0 else set[str]()
                assert visible_skus(config, base, database, filtered) == expected
        assert visible_skus(config, base, old) == original


def test_invalid_proposals_leave_the_existing_product_intact(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    base = releases["base"]
    duplicate = SeedProposal(model=MUG.model, fields={**MUG.fields, "sku": "PEN"})
    wrong_field = SeedProposal(model=MUG.model, fields={**MUG.fields, "amount": 5})
    with managed_postgres(config) as server:
        database = prepare_old_snapshot(
            config, base, server, settings_module=SETTINGS, seed_fixture="products",
        )
        for proposal, field in ((duplicate, "sku"), (wrong_field, "amount")):
            result = insert_seed_proposal(
                config, base, database, settings_module=SETTINGS, proposal=proposal,
            )
            assert result.status == "exception"
            assert field in (result.exception_message or "").lower()
        assert visible_skus(config, base, database) == {"PEN", "NOTEBOOK"}
        assert not visible_skus(config, base, database, "/products/?out_of_stock=1")
