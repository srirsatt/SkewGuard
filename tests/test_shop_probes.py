"""Exercise the committed shop fixture and its ORM read probe against PostgreSQL."""

import json
import os
import shutil
from pathlib import Path

import pytest
from psycopg import sql

from skewguard.config import RunConfig
from skewguard.exec.db import clone_database, connect_database, managed_postgres
from skewguard.exec.envs import (
    PythonEnvironment,
    create_environment,
    install_dependencies,
)
from skewguard.exec.probes import run_read_probes, seed_database
from skewguard.exec.runner import run_migrations

pytestmark = pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 to run tests requiring Docker",
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def shop_releases(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[RunConfig, dict[str, PythonEnvironment]]:
    root = tmp_path_factory.mktemp("shop-releases")
    config = RunConfig(repo_path=root, base_ref="base", head_ref="head")
    environments: dict[str, PythonEnvironment] = {}
    for release in ("base", "rename", "nullable"):
        checkout = root / release
        shutil.copytree(
            ROOT / "benchmark/apps/shop",
            checkout,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".skewguard-venv"),
        )
        if release != "base":
            case = "shop_rename_price" if release == "rename" else "shop_add_notes"
            shutil.copyfile(
                ROOT / "benchmark/cases" / f"{case}.py",
                checkout / "catalog/migrations/0002_change.py",
            )
            model = checkout / "catalog/models.py"
            if release == "rename":
                for path in (model, checkout / "catalog/views.py"):
                    path.write_text(path.read_text().replace("price", "amount"))
            else:
                model.write_text(
                    model.read_text().replace(
                        "    class Meta:",
                        "    notes = models.TextField(null=True)\n\n    class Meta:",
                    )
                )
        environment = create_environment(config, checkout)
        install_dependencies(config, environment)
        environments[release] = environment
    return config, environments


def test_shop_fixture_seeds_and_both_head_migrations_preserve_rows(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    labels = json.loads((ROOT / "benchmark/cases/shop.json").read_text())
    assert {case["id"] for case in labels} == {"shop_rename_price", "shop_add_notes"}
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
