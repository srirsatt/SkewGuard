"""Check connection handoff and timeout settings without requiring Docker."""

from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from pydantic import SecretStr

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection, connect_database


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
