"""Exercise the committed shop fixture and its ORM read probe against PostgreSQL."""

import json
import os
from pathlib import Path

import pytest
from psycopg import sql

from skewguard.config import RunConfig
from skewguard.exec.db import clone_database, connect_database, managed_postgres
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import (
    ProbeExecutionError,
    run_committed_writes,
    run_read_probes,
    run_write_probes,
    seed_database,
)
from skewguard.exec.runner import run_migrations

pytestmark = pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 to run tests requiring Docker",
)

ROOT = Path(__file__).resolve().parents[1]


def test_shop_fixture_seeds_and_both_head_migrations_preserve_rows(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    labels = json.loads((ROOT / "benchmark/cases/shop.json").read_text())
    assert {case["id"] for case in labels} == {
        "shop_rename_price",
        "shop_add_notes",
        "shop_add_required_category",
    }
    with managed_postgres(config) as server:
        baseline = clone_database(config, server)
        run_migrations(
            config, releases["base"], baseline, settings_module="shop.settings"
        )
        seed_database(
            config,
            releases["base"],
            baseline,
            settings_module="shop.settings",
            fixture="products",
        )
        for release, column in (("rename", "amount"), ("nullable", "price")):
            copied = clone_database(config, server, source=baseline.dbname)
            run_migrations(
                config, releases[release], copied, settings_module="shop.settings"
            )
            with connect_database(config, copied) as connection:
                assert connection.execute(
                    sql.SQL("SELECT sku, {} FROM catalog_product ORDER BY id").format(
                        sql.Identifier(column)
                    )
                ).fetchall() == [("NOTEBOOK", 20), ("PEN", 5)]

        with connect_database(config, baseline) as connection:
            assert connection.execute(
                "SELECT sku, price FROM catalog_product ORDER BY id"
            ).fetchall() == [("NOTEBOOK", 20), ("PEN", 5)]


def test_read_probe_detects_rename_and_accepts_nullable_addition(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    with managed_postgres(config) as server:
        baseline = clone_database(config, server)
        run_migrations(
            config, releases["base"], baseline, settings_module="shop.settings"
        )
        seed_database(
            config,
            releases["base"],
            baseline,
            settings_module="shop.settings",
            fixture="products",
        )
        limited = config.model_copy(update={"probe_row_limit": 1})
        baseline_outcomes = run_read_probes(
            limited, releases["base"], baseline, settings_module="shop.settings"
        )
        assert len(baseline_outcomes) == 1
        baseline_read = baseline_outcomes[0]
        assert baseline_read.scenario_id == "orm:catalog.Product:read"
        assert baseline_read.status == "ok"
        assert baseline_read.body == [
            {"id": 2, "sku": "PEN", "name": "Pen", "price": 5, "stock": 30}
        ]

        for release in ("rename", "nullable"):
            copied = clone_database(config, server, source=baseline.dbname)
            run_migrations(
                config, releases[release], copied, settings_module="shop.settings"
            )
            old_read = run_read_probes(
                limited, releases["base"], copied, settings_module="shop.settings"
            )[0]
            if release == "rename":
                assert old_read.status == "exception"
                assert old_read.exception_type == "ProgrammingError"
                assert "price" in (old_read.exception_message or "")
                assert not old_read.integrity_error
            else:
                assert old_read == baseline_read
            new_read = run_read_probes(
                limited, releases[release], copied, settings_module="shop.settings"
            )[0]
            assert new_read.status == "ok"


def test_write_probe_catches_required_column_and_rolls_back_inserted_rows(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    with managed_postgres(config) as server:
        baseline = clone_database(config, server)
        run_migrations(
            config, releases["base"], baseline, settings_module="shop.settings"
        )
        seed_database(
            config,
            releases["base"],
            baseline,
            settings_module="shop.settings",
            fixture="products",
        )
        for release in ("base", "nullable", "required"):
            database = baseline
            if release != "base":
                database = clone_database(config, server, source=baseline.dbname)
                run_migrations(
                    config, releases[release], database, settings_module="shop.settings"
                )
            with connect_database(config, database) as connection:
                before = connection.execute(
                    "SELECT id, sku, name, price, stock "
                    "FROM catalog_product ORDER BY id"
                ).fetchall()

            old_read = run_read_probes(
                config, releases["base"], database, settings_module="shop.settings"
            )[0]
            assert old_read.status == "ok"

            for _ in range(2):
                old_write = run_write_probes(
                    config, releases["base"], database, settings_module="shop.settings"
                )[0]
                assert old_write.scenario_id == "orm:catalog.Product:write"
                if release == "required":
                    assert old_write.status == "exception"
                    assert old_write.exception_type == "IntegrityError"
                    assert old_write.integrity_error
                    assert "category" in (old_write.exception_message or "")
                else:
                    assert old_write.status == "ok"
                    assert old_write.body == {"created": True, "rolled_back": True}

            new_write = run_write_probes(
                config, releases[release], database, settings_module="shop.settings"
            )[0]
            assert new_write.status == "ok"

            with connect_database(config, database) as connection:
                assert (
                    connection.execute(
                        "SELECT id, sku, name, price, stock "
                        "FROM catalog_product ORDER BY id"
                    ).fetchall()
                    == before
                )


def test_committed_writes_persist_and_failed_batches_leave_no_rows(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    with managed_postgres(config) as server:
        database = clone_database(config, server)
        run_migrations(
            config, releases["base"], database, settings_module="shop.settings"
        )
        seed_database(
            config,
            releases["base"],
            database,
            settings_module="shop.settings",
            fixture="products",
        )
        run_migrations(
            config, releases["required"], database, settings_module="shop.settings"
        )
        with pytest.raises(ProbeExecutionError, match="IntegrityError"):
            run_committed_writes(
                config, releases["base"], database, settings_module="shop.settings"
            )
        with connect_database(config, database) as connection:
            count = connection.execute(
                "SELECT count(*) FROM catalog_product"
            ).fetchone()
            assert count == (2,)

        committed = run_committed_writes(
            config, releases["required"], database, settings_module="shop.settings"
        )
        assert len(committed) == 1
        assert committed[0].scenario_id == "orm:catalog.Product:commit"
        assert committed[0].body == {"created": True, "rolled_back": False}
        with connect_database(config, database) as connection:
            count = connection.execute(
                "SELECT count(*) FROM catalog_product"
            ).fetchone()
            assert count == (3,)
            assert connection.execute(
                "SELECT category FROM catalog_product WHERE sku NOT IN (%s, %s)",
                ("PEN", "NOTEBOOK"),
            ).fetchone() == ("general",)
