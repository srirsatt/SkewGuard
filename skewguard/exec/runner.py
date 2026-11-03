"""Prepare isolated subprocess inputs for a checkout's Django migrations."""

import json
import os

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection


class MigrationError(RuntimeError):
    """A checkout could not apply its migrations to the sandbox database."""


def migration_input(
    config: RunConfig, database: DatabaseConnection, settings_module: str
) -> str:
    """Encode the sandbox settings for the child's stdin, including credentials."""
    if not settings_module or not all(
        part.isidentifier() for part in settings_module.split(".")
    ):
        raise MigrationError("settings_module must be a dotted Python module name")

    return json.dumps(
        {
            "settings_module": settings_module,
            "database": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": database.dbname,
                "HOST": database.host,
                "PORT": database.port,
                "USER": database.username,
                "PASSWORD": database.password.get_secret_value(),
                "CONN_MAX_AGE": 0,
                "OPTIONS": {
                    "connect_timeout": config.timeout_seconds,
                    "options": f"-c statement_timeout={config.timeout_seconds * 1000}",
                },
            },
        }
    )


def migration_environment() -> dict[str, str]:
    """Keep application configuration while discarding inherited Python selectors."""
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "DJANGO_SETTINGS_MODULE"):
        environment.pop(name, None)
    return environment


def redact_database_password(text: str, database: DatabaseConnection) -> str:
    """Remove the exact sandbox password from returned logs and error messages."""
    password = database.password.get_secret_value()
    return text.replace(password, "[redacted]") if password else text
