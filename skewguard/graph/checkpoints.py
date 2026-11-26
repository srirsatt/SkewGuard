"""SQLite checkpoints with explicit schema revival and per-run process locks."""

import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path

from filelock import FileLock, Timeout
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

from skewguard import config
from skewguard.agents import scenario as agent_schemas
from skewguard.exec import scenarios
from skewguard.exec import types as evidence
from skewguard.graph import state
from skewguard.report import json_report
from skewguard.static import types as analysis

DEFAULT_CHECKPOINT = Path(".skewguard/checkpoints.sqlite")


class CheckpointError(ValueError):
    """A checkpoint cannot be safely opened or resumed."""


def run_config(run_id: str) -> RunnableConfig:
    """Use canonical IDs for isolated threads and bounded graph invocations."""
    if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise CheckpointError("run ID must contain 32 lowercase hexadecimal characters")
    return {"configurable": {"thread_id": run_id}, "recursion_limit": 100}


def checkpoint_serializer() -> JsonPlusSerializer:
    """Permit our declared schemas plus framework built-ins, with no pickle."""
    allowed: list[tuple[str, str]] = []
    modules = (config, evidence, scenarios, state, json_report, analysis, agent_schemas)
    for module in modules:
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and value.__module__ == module.__name__
                and issubclass(value, (BaseModel, Enum))
            ):
                allowed.append((value.__module__, value.__name__))
    return JsonPlusSerializer(
        allowed_msgpack_modules=allowed,
        allowed_json_modules=[(*module.split("."), name) for module, name in allowed],
        pickle_fallback=False,
    )


@contextmanager
def open_checkpoints(
    path: Path, run_id: str, *, create: bool
) -> Generator[SqliteSaver, None, None]:
    """Own the connection and a crash-released lock for this checkpoint thread.

    A store marker prevents treating arbitrary SQLite files as checkpoint stores.
    Separate run IDs may share a database; simultaneous use of one ID is rejected.
    """
    run_config(run_id)
    path = path.expanduser().resolve()
    if not create and not path.is_file():
        raise CheckpointError(f"checkpoint database does not exist: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(path.parent / f".{path.name}.{run_id}.lock", timeout=0)
    try:
        lock.acquire()
    except Timeout as error:
        raise CheckpointError(
            "this run is already active in another process"
        ) from error
    try:
        connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "skewguard_store" not in tables and (tables or not create):
                raise CheckpointError("file is not a SkewGuard checkpoint database")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS skewguard_store "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
            )
            connection.execute("INSERT OR IGNORE INTO skewguard_store VALUES (1, 1)")
            versions = connection.execute(
                "SELECT id, version FROM skewguard_store"
            ).fetchall()
            if versions != [(1, 1)]:
                raise CheckpointError("unsupported checkpoint store version")
            connection.commit()
            serde = checkpoint_serializer()
            saver = SqliteSaver(connection, serde=serde)
            saver.jsonplus_serde = serde
            yield saver
        finally:
            connection.close()
    finally:
        lock.release()
