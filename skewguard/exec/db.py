"""Connection settings and helpers for disposable PostgreSQL databases."""

from secrets import token_urlsafe

import psycopg
from uuid import uuid4
from psycopg import sql
from psycopg.rows import TupleRow
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from testcontainers.community.postgres import PostgresContainer

from skewguard.config import RunConfig
from collections.abc import Generator
from contextlib import ExitStack, contextmanager

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

# connect the helpers with managed_postgres() -> return the connection details, connect the db and start it up

@contextmanager
def managed_postgres(config: RunConfig) -> Generator[DatabaseConnection, None, None]:
    # start up postgres server and remove on caller exit
    with ExitStack() as cleanup:
        try:
            postgres = make_postgres_container(config)
            cleanup.callback(postgres.stop)
            postgres.start()

            database = DatabaseConnection(
                host=postgres.get_container_host_ip(),
                port = int(postgres.get_exposed_port(5432)),
                username = postgres.username,
                password=SecretStr(postgres.password),
                dbname=postgres.dbname,

            )
            with connect_database(config, database) as connection:
                connection.execute("SELECT 1")

        except Exception as error:
            raise DatabaseSetupError("couldn't prepare temp PostgreSQL") from error
        
        yield database


# clones db with data
def clone_database(config: RunConfig, server: DatabaseConnection, *, source: str = "template0") -> DatabaseConnection:
    target = server.model_copy(update={"dbname": f"skewguard_{uuid4().hex}"})
    admin = server.model_copy(update={"dbname": "postgres"})

    statement = sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
        sql.Identifier(target.dbname),
        sql.Identifier(source),
    )

    try:
        with connect_database(config, admin, autocommit=True) as connection:
            connection.execute(statement)
    except psycopg.Error as error:
        raise DatabaseSetupError(f"could not clone database {source!r}") from error

    return target


