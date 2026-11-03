"""Check connection handoff and timeout settings without requiring Docker."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from pydantic import SecretStr

from skewguard.config import RunConfig
from skewguard.exec.db import (
    DatabaseConnection,
    DatabaseSetupError,
    connect_database,
    managed_postgres,
)


@pytest.fixture
def database() -> DatabaseConnection:
    return DatabaseConnection(
        host="127.0.0.1",
        port=54321,
        username="skewguard",
        password=SecretStr("password with ' quotes / and @"),
        dbname="baseline",
    )


def test_connection_details_redact_passwords(database: DatabaseConnection) -> None:
    password = database.password.get_secret_value()

    assert password not in repr(database)
    assert password not in database.model_dump_json()


@pytest.mark.parametrize("autocommit", [False, True])
def test_connect_database_passes_credentials_and_timeouts(
    tmp_path: Path, database: DatabaseConnection, autocommit: bool
) -> None:
    config = RunConfig(
        repo_path=tmp_path, base_ref="main", head_ref="HEAD", timeout_seconds=7
    )
    with patch("skewguard.exec.db.psycopg.connect", autospec=True) as connect:
        connection = connect_database(config, database, autocommit=autocommit)

        connect.assert_called_once_with(
            host="127.0.0.1",
            port=54321,
            user="skewguard",
            password=database.password.get_secret_value(),
            dbname="baseline",
            connect_timeout=7,
            options="-c statement_timeout=7000",
            autocommit=autocommit,
        )
        assert connection is connect.return_value


def test_connection_failures_are_preserved(
    tmp_path: Path, database: DatabaseConnection
) -> None:
    config = RunConfig(repo_path=tmp_path, base_ref="main", head_ref="HEAD")
    failure = psycopg.OperationalError("connection refused")
    with patch("skewguard.exec.db.psycopg.connect", side_effect=failure):
        with pytest.raises(psycopg.OperationalError) as raised:
            connect_database(config, database)

    assert raised.value is failure


@pytest.fixture
def postgres() -> MagicMock:
    container = MagicMock()
    container.username = "skewguard"
    container.password = "fixture-secret"
    container.dbname = "postgres"
    container.get_container_host_ip.return_value = "127.0.0.1"
    container.get_exposed_port.return_value = "54321"
    return container


def test_managed_postgres_checks_connection_and_cleans_up(
    tmp_path: Path, postgres: MagicMock
) -> None:
    config = RunConfig(repo_path=tmp_path, base_ref="main", head_ref="HEAD")
    with (
        patch("skewguard.exec.db.make_postgres_container", return_value=postgres),
        patch("skewguard.exec.db.connect_database", autospec=True) as connect,
    ):
        with managed_postgres(config) as database:
            assert database.host == "127.0.0.1"
            assert database.port == 54321
            assert database.password.get_secret_value() == "fixture-secret"
            assert database.dbname == "postgres"
            postgres.start.assert_called_once_with()
            postgres.stop.assert_not_called()
            connect.assert_called_once_with(config, database)
            session = connect.return_value.__enter__.return_value
            session.execute.assert_called_once_with("SELECT 1")
            connect.return_value.__exit__.assert_called_once()

        postgres.stop.assert_called_once_with()


@pytest.mark.parametrize("stage", ["start", "connect", "query"])
def test_managed_postgres_cleans_up_after_setup_failure(
    tmp_path: Path, postgres: MagicMock, stage: str
) -> None:
    config = RunConfig(repo_path=tmp_path, base_ref="main", head_ref="HEAD")
    failure = RuntimeError(f"{stage} failed")
    with (
        patch("skewguard.exec.db.make_postgres_container", return_value=postgres),
        patch("skewguard.exec.db.connect_database", autospec=True) as connect,
    ):
        if stage == "start":
            postgres.start.side_effect = failure
        elif stage == "connect":
            connect.side_effect = failure
        else:
            connect.return_value.__enter__.return_value.execute.side_effect = failure

        with pytest.raises(DatabaseSetupError) as raised:
            with managed_postgres(config):
                pytest.fail("setup failed, so the caller must not run")

        assert raised.value.__cause__ is failure
        postgres.stop.assert_called_once_with()


def test_managed_postgres_preserves_caller_errors(
    tmp_path: Path, postgres: MagicMock
) -> None:
    config = RunConfig(repo_path=tmp_path, base_ref="main", head_ref="HEAD")
    failure = ValueError("migration failed")
    with (
        patch("skewguard.exec.db.make_postgres_container", return_value=postgres),
        patch("skewguard.exec.db.connect_database", autospec=True),
    ):
        with pytest.raises(ValueError) as raised:
            with managed_postgres(config):
                raise failure

        assert raised.value is failure
        postgres.stop.assert_called_once_with()


def test_managed_postgres_reports_container_construction_errors(tmp_path: Path) -> None:
    config = RunConfig(repo_path=tmp_path, base_ref="main", head_ref="HEAD")
    failure = RuntimeError("Docker unavailable")
    with patch("skewguard.exec.db.make_postgres_container", side_effect=failure):
        with pytest.raises(DatabaseSetupError) as raised:
            with managed_postgres(config):
                pytest.fail("no container was constructed")

    assert raised.value.__cause__ is failure
