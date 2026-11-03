"""Prepare isolated subprocess inputs for a checkout's Django migrations."""

import json
import os

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection

import subprocess

from skewguard.exec.envs import (
    PythonEnvironment,
    backend_directory,
    validate_environment,
)

MIGRATION_SCRIPT = """
import json
import os
import sys

request = json.load(sys.stdin)
sys.path.insert(0, sys.argv[1])
os.environ["DJANGO_SETTINGS_MODULE"] = request["settings_module"]

import django
from django.conf import settings
from django.core.management import call_command
from django.db import connections

if set(settings.DATABASES) != {"default"} or settings.DATABASE_ROUTERS:
    raise RuntimeError("Migration runner requires one default database without routers")

settings.DATABASES = {"default": request["database"]}

try:
    django.setup()
    call_command(
        "migrate", database="default", interactive=False,
        verbosity=1, skip_checks=False,
    )
finally:
    connections.close_all()
"""


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

def run_migrations(config: RunConfig, environment: PythonEnvironment, database: DatabaseConnection, *, settings_module: str) -> str:
    validate_environment(environment)
    backend = backend_directory(config, environment.checkout)
    payload = migration_input(config, database, settings_module)

    try:
        result = subprocess.run(
            [str(environment.python), "-I", "-c", MIGRATION_SCRIPT, str(backend)],
            cwd=backend,
            env=migration_environment(),
            input=payload,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "migration failed"
        detail = redact_database_password(detail, database)
        raise MigrationError(f"Django migrate failed: {detail}") from error 
    except subprocess.TimeoutExpired as error:
        raise MigrationError(f"Django migrate timed out after {config.timeout_seconds}s") from error
    except OSError as error:
        raise MigrationError(f"could not start Django: {error}") from error

    return redact_database_password(result.stdout.strip(), database)
