"""Check the JSON request on the wire and the effect of a real shop POST."""

import json
import os

import pytest

from skewguard.config import RunConfig
from skewguard.exec._probe_worker import encode_post_body
from skewguard.exec.db import clone_database, connect_database, managed_postgres
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import run_http_scenario, seed_database
from skewguard.exec.runner import run_migrations
from skewguard.exec.scenarios import Scenario


def test_post_body_preserves_json_types_in_utf8_bytes() -> None:
    for body in (
        {"name": "Café", "active": False},
        [None, 0],
        "quoted",
        False,
        0,
        None,
    ):
        wire = encode_post_body(body)
        assert isinstance(wire, bytes)
        assert json.dumps(json.loads(wire.decode("utf-8"))) == json.dumps(body)
    with pytest.raises(ValueError):
        encode_post_body({"price": float("nan")})


@pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 for Docker",
)
def test_shop_post_commits_only_valid_products(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    base = releases["base"]
    settings = "shop.settings"
    product: dict[str, object] = {
        "sku": "MUG",
        "name": "Café mug",
        "price": 12,
        "stock": 4,
    }
    with managed_postgres(config) as server:
        db = clone_database(config, server)
        run_migrations(config, base, db, settings_module=settings)
        seed_database(config, base, db, settings_module=settings, fixture="products")

        def post(body: dict[str, object]):
            scenario = Scenario.model_validate(
                {
                    "scenario_id": "http:catalog.products:create",
                    "method": "POST",
                    "path": "/products/",
                    "kind": "write",
                    "body": body,
                    "compare_fields": ["price"],
                }
            )
            return run_http_scenario(
                config, base, db, settings_module=settings, scenario=scenario
            )

        created = post(product)
        assert created.scenario_id == "http:catalog.products:create"
        assert created.status == "http_status" and created.http_status == 201
        assert created.body == product
        invalid = post({"sku": "INVALID", "name": "Missing price"})
        assert invalid.status == "http_status" and invalid.http_status == 400
        duplicate = post({**product, "price": 99})
        assert duplicate.http_status == 400

        with connect_database(config, db) as connection:
            assert connection.execute(
                "SELECT sku, name, price, stock FROM catalog_product WHERE sku = %s",
                ("MUG",),
            ).fetchall() == [("MUG", "Café mug", 12, 4)]
            assert connection.execute(
                "SELECT sku FROM catalog_product ORDER BY sku"
            ).fetchall() == [("MUG",), ("NOTEBOOK",), ("PEN",)]
