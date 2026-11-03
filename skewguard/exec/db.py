"""Connection settings and helpers for disposable PostgreSQL databases."""

from secrets import token_urlsafe

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from testcontainers.community.postgres import PostgresContainer

from skewguard.config import RunConfig

POSTGRES_IMAGE = "postgres:16"


class DatabaseSetupError(RuntimeError):
    """A disposable database could not be prepared."""


class DatabaseConnection(BaseModel):
    """Connection details for one database, with passwords hidden in repr/JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1)
    port: int = Field(gt=0, le=65535, strict=True)
    username: str = Field(min_length=1)
    password: SecretStr
    dbname: str = Field(min_length=1)


def make_postgres_container(config: RunConfig) -> PostgresContainer:
    """Configure a run-owned server; the caller owns startup and cleanup."""
    return PostgresContainer(
        POSTGRES_IMAGE,
        username="skewguard",
        password=token_urlsafe(24),
        dbname="postgres",
        driver=None,
        docker_client_kw={"timeout": config.timeout_seconds},
    )


def connect_database(
    config: RunConfig,
    database: DatabaseConnection,
    *,
    autocommit: bool = False,
) -> psycopg.Connection[TupleRow]:
    """Open a session with connection and statement timeouts; callers close it."""
    return psycopg.connect(
        host=database.host,
        port=database.port,
        user=database.username,
        password=database.password.get_secret_value(),
        dbname=database.dbname,
        connect_timeout=config.timeout_seconds,
        options=f"-c statement_timeout={config.timeout_seconds * 1000}",
        autocommit=autocommit,
    )
