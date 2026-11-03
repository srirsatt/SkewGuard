"""Check migration input boundaries before invoking a checkout's interpreter."""

import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection
from skewguard.exec.runner import (
    MigrationError,
    migration_environment,
    migration_input,
    redact_database_password,
)


@pytest.fixture
def database() -> DatabaseConnection:
    return DatabaseConnection(
        host="127.0.0.1",
        port=54321,
        username="skewguard",
        password=SecretStr("secret with ' quotes / and @"),
        dbname="skewguard_baseline",
    )


def test_migration_input_preserves_credentials_and_sets_database_timeouts(
    tmp_path: Path, database: DatabaseConnection
) -> None:
    config = RunConfig(
        repo_path=tmp_path, base_ref="main", head_ref="HEAD", timeout_seconds=7
    )

    payload = json.loads(migration_input(config, database, "shop.settings"))

    assert payload["settings_module"] == "shop.settings"
    assert payload["database"] == {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": database.dbname,
        "HOST": database.host,
        "PORT": database.port,
        "USER": database.username,
        "PASSWORD": database.password.get_secret_value(),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 7, "options": "-c statement_timeout=7000"},
    }


@pytest.mark.parametrize("module", ["", ".settings", "shop..settings", "shop/settings"])
def test_migration_input_rejects_invalid_settings_modules(
    tmp_path: Path, database: DatabaseConnection, module: str
) -> None:
    config = RunConfig(repo_path=tmp_path, base_ref="main", head_ref="HEAD")

    with pytest.raises(MigrationError, match="dotted Python module"):
        migration_input(config, database, module)


def test_child_environment_discards_selectors_without_changing_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selectors = ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "DJANGO_SETTINGS_MODULE")
    for name in selectors:
        monkeypatch.setenv(name, "parent-value")
    monkeypatch.setenv("SKEWGUARD_FIXTURE_SETTING", "application-value")

    child = migration_environment()

    assert all(name not in child for name in selectors)
    assert all(os.environ[name] == "parent-value" for name in selectors)
    assert child["SKEWGUARD_FIXTURE_SETTING"] == "application-value"


def test_migration_logs_redact_sandbox_password(database: DatabaseConnection) -> None:
    password = database.password.get_secret_value()

    output = redact_database_password(f"connection failed: {password}", database)

    assert password not in output
    assert output == "connection failed: [redacted]"
