"""Exercise real PostgreSQL copies and cleanup when Docker tests are enabled."""

import os
from pathlib import Path

import psycopg
import pytest

from skewguard.config import RunConfig
from skewguard.exec.db import (
    DatabaseConnection,
    DatabaseSetupError,
    clone_database,
    connect_database,
    managed_postgres,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 to run tests requiring Docker",
)


@pytest.fixture
def config(tmp_path: Path) -> RunConfig:
    return RunConfig(repo_path=tmp_path, base_ref="main", head_ref="HEAD")


def test_clones_preserve_schema_rows_and_sequences_without_sharing_changes(
    config: RunConfig,
) -> None:
    with managed_postgres(config) as server:
        baseline = clone_database(config, server)
        with connect_database(config, baseline) as connection:
            assert connection.execute("SELECT to_regclass('products')").fetchone() == (
                None,
            )
            connection.execute(
                "CREATE TABLE products ("
                "id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                "name text NOT NULL, price integer NOT NULL)"
            )
            connection.execute(
                "INSERT INTO products (name, price) VALUES (%s, %s)",
                ("Notebook", 20),
            )

        migrated = clone_database(config, server, source=baseline.dbname)
        sibling = clone_database(config, server, source=baseline.dbname)
        assert len({baseline.dbname, migrated.dbname, sibling.dbname}) == 3

        with connect_database(config, migrated) as connection:
            assert connection.execute(
                "SELECT id, name, price FROM products"
            ).fetchall() == [(1, "Notebook", 20)]
            connection.execute("ALTER TABLE products RENAME COLUMN price TO amount")
            connection.execute("UPDATE products SET amount = 99 WHERE id = 1")
            assert connection.execute(
                "INSERT INTO products (name, amount) VALUES (%s, %s) RETURNING id",
                ("Pen", 5),
            ).fetchone() == (2,)

        for unchanged in (baseline, sibling):
            with connect_database(config, unchanged) as connection:
                assert connection.execute(
                    "SELECT id, name, price FROM products ORDER BY id"
                ).fetchall() == [(1, "Notebook", 20)]

        with connect_database(config, sibling) as connection:
            assert connection.execute(
                "INSERT INTO products (name, price) VALUES (%s, %s) RETURNING id",
                ("Pencil", 3),
            ).fetchone() == (2,)

        with connect_database(config, migrated) as connection:
            assert connection.execute(
                "SELECT id, name, amount FROM products ORDER BY id"
            ).fetchall() == [(1, "Notebook", 99), (2, "Pen", 5)]

        with connect_database(config, baseline) as connection:
            assert connection.execute(
                "SELECT id, name, price FROM products ORDER BY id"
            ).fetchall() == [(1, "Notebook", 20)]

    short_timeout = config.model_copy(update={"timeout_seconds": 2})
    with pytest.raises(psycopg.OperationalError):
        with connect_database(short_timeout, server):
            pytest.fail("the removed server must not accept new connections")


def test_cloning_requires_the_source_connections_to_be_closed(
    config: RunConfig,
) -> None:
    with managed_postgres(config) as server:
        baseline = clone_database(config, server)
        with connect_database(config, baseline) as connection:
            connection.execute("SELECT 1")
            with pytest.raises(DatabaseSetupError) as raised:
                clone_database(config, server, source=baseline.dbname)
            assert isinstance(raised.value.__cause__, psycopg.errors.ObjectInUse)

        copied = clone_database(config, server, source=baseline.dbname)
        with connect_database(config, copied) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)


def test_caller_failure_still_removes_the_postgres_server(config: RunConfig) -> None:
    failure = ValueError("scenario failed")
    server: DatabaseConnection | None = None
    with pytest.raises(ValueError) as raised:
        with managed_postgres(config) as server:
            raise failure

    assert raised.value is failure
    assert server is not None
    short_timeout = config.model_copy(update={"timeout_seconds": 2})
    with pytest.raises(psycopg.OperationalError):
        with connect_database(short_timeout, server):
            pytest.fail("the removed server must not accept new connections")
