"""Exercise Django's session middleware and seeded users in a real checkout."""

import os

import pytest

from skewguard.config import RunConfig
from skewguard.exec.db import clone_database, connect_database, managed_postgres
from skewguard.exec.diff import probe_input_is_unavailable
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import run_http_get, run_http_scenario, seed_database
from skewguard.exec.runner import run_migrations
from skewguard.exec.scenarios import HttpMethod, Scenario
from skewguard.exec.types import Outcome


@pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 for Docker",
)
def test_account_receives_the_requested_seeded_identity(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    base = releases["base"]
    settings = "shop.auth_settings"
    with managed_postgres(config) as server:
        db = clone_database(config, server)
        run_migrations(config, base, db, settings_module=settings)
        seed_database(config, base, db, settings_module=settings, fixture="buyers")

        def account(identifier: str | None, method: HttpMethod = "GET") -> Outcome:
            scenario = Scenario(
                scenario_id=f"http:account:{method}:{identifier or 'guest'}",
                method=method,
                path="/account/",
                kind="read" if method == "GET" else "write",
                as_user=identifier,
            )
            execute = run_http_get if method == "GET" else run_http_scenario
            return execute(
                config, base, db, settings_module=settings, scenario=scenario
            )

        assert account(None).http_status == 403
        buyer = account("buyer")
        assert buyer.http_status == 200, buyer
        assert buyer.body == {"username": "buyer"}
        seller = account("seller", "POST")
        assert seller.http_status == 200, seller
        assert seller.body == {"username": "seller"}
        assert account(None).http_status == 403

        missing = account("nobody")
        assert probe_input_is_unavailable(missing), missing
        with connect_database(config, db) as connection:
            assert connection.execute(
                "SELECT username FROM auth_user ORDER BY username"
            ).fetchall() == [("buyer",), ("seller",)]
            connection.execute("UPDATE auth_user SET is_active = false WHERE id = 2")
        assert probe_input_is_unavailable(account("seller"))

        with connect_database(config, db) as connection:
            connection.execute("ALTER TABLE auth_user RENAME COLUMN username TO handle")
        broken = account("buyer")
        assert broken.status == "exception"
        assert broken.exception_type == "ProgrammingError"
