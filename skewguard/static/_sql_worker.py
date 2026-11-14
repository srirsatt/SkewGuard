"""Collect Django SQL on an inspection database, advancing only between records."""

import json
import os
import sys
from contextlib import redirect_stdout
from importlib import import_module
from io import StringIO
from pathlib import Path
from typing import Any


def key_record(key: tuple[str, str]) -> dict[str, str]:
    return {"app_label": key[0], "name": key[1]}


def unavailable(key: tuple[str, str], detail: str) -> dict[str, object]:
    return {"migration": key_record(key), "status": "unavailable", "detail": detail}


def sql_coverage(migration: Any) -> tuple[bool, list[str]]:
    """Skip custom/wrapped SQL hooks and flag behavior that SQL cannot fully show."""
    operations = import_module("django.db.migrations.operations")
    postgres = import_module("django.contrib.postgres.operations")
    partial: list[str] = []
    not_provided = import_module("django.db.models").NOT_PROVIDED
    for operation in migration.operations:
        name = type(operation).__name__
        if type(operation) not in {
            getattr(operations, name, None),
            getattr(postgres, name, None),
        }:
            return False, [
                f"Custom operation {type(operation).__module__}.{name} "
                "is not collected."
            ]
        if name == "SeparateDatabaseAndState":
            return False, [
                "Wrapped database/state operations need separate SQL inspection."
            ]
        if not operation.reduces_to_sql:
            partial.append(f"{name} behavior is not represented as SQL.")
        fields = [getattr(operation, "field", None)]
        if name == "CreateModel":
            fields.extend(field for _, field in operation.fields)
        if any(
            (default := getattr(field, "default", not_provided)) is not not_provided
            and callable(default)
            for field in fields
            if field is not None
        ):
            partial.append(
                "Callable defaults may produce different SQL on reevaluation."
            )
    return True, list(dict.fromkeys(partial))


def collect_sql_records(
    connection: Any, keys: list[tuple[str, str]]
) -> dict[str, object]:
    """Generate each migration's SQL using the schema immediately before it.

    Earlier pending migrations are applied to this disposable database so later
    introspection can resolve real constraint names. The final migration is not
    applied, since no following SQL needs that state. No compatibility verdicts
    are inferred from collection or advancement failures.
    """
    executor_type = import_module("django.db.migrations.executor").MigrationExecutor
    call_command = import_module("django.core.management").call_command
    executor = executor_type(connection)
    executor.loader.check_consistent_history(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    actual = [(migration.app_label, migration.name) for migration, _ in plan]
    if any(backwards for _, backwards in plan) or actual != keys:
        detail = (
            "Inspection database migration plan does not match the selected migrations."
        )
        return {
            "results": [unavailable(key, detail) for key in keys],
            "issues": [{"kind": "sql_state_unavailable", "message": detail}],
        }

    results: list[dict[str, object]] = []
    issues: list[dict[str, object]] = []
    blocked: str | None = None
    for index, key in enumerate(keys):
        if blocked is not None:
            results.append(unavailable(key, blocked))
            issues.append(
                {
                    "kind": "sql_unavailable",
                    "migration": key_record(key),
                    "message": blocked,
                }
            )
            continue
        migration = executor.loader.graph.nodes[key]
        try:
            supported, gaps = sql_coverage(migration)
            if not supported:
                raise ValueError(" ".join(gaps))
            output = StringIO()
            call_command(
                "sqlmigrate",
                *key,
                database="default",
                stdout=output,
                no_color=True,
                verbosity=0,
            )
            detail = " ".join(gaps) if gaps else None
            results.append(
                {
                    "migration": key_record(key),
                    "status": "partial" if gaps else "collected",
                    "sql": output.getvalue(),
                    "detail": detail,
                }
            )
            if detail:
                issues.append(
                    {
                        "kind": "sql_partial",
                        "migration": key_record(key),
                        "message": detail,
                    }
                )
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            results.append(unavailable(key, detail))
            issues.append(
                {
                    "kind": "sql_unavailable",
                    "migration": key_record(key),
                    "message": detail,
                }
            )
            connection.close()

        if index + 1 < len(keys):
            try:
                advance = executor_type(connection)
                advance_plan = advance.migration_plan([key])
                if [
                    (item.app_label, item.name, backwards)
                    for item, backwards in advance_plan
                ] != [(*key, False)]:
                    raise ValueError(
                        "Inspection advancement would apply an unexpected plan."
                    )
                advance.migrate([key], plan=advance_plan)
            except Exception as error:
                blocked = (
                    f"SQL schema context unavailable after {key[0]}.{key[1]}: "
                    f"{type(error).__name__}: {error}"
                )
                issues.append(
                    {
                        "kind": "sql_state_unavailable",
                        "migration": key_record(key),
                        "message": blocked,
                    }
                )
                connection.close()
    return {"results": results, "issues": issues}


def main() -> None:
    request = json.load(sys.stdin)
    keys = [(item["app_label"], item["name"]) for item in request["migrations"]]
    if len(keys) != len(set(keys)):
        raise ValueError("selected migrations must have unique identities")
    module = request["settings_module"]
    if not module or not all(part.isidentifier() for part in module.split(".")):
        raise ValueError("settings_module must be a dotted Python module name")
    backend = Path(sys.argv[1]).resolve()
    checkout = Path(sys.argv[2]).resolve()
    if not backend.is_dir() or not backend.is_relative_to(checkout):
        raise ValueError("backend must be a directory within the checkout")
    sys.path.insert(0, str(backend))
    os.environ["DJANGO_SETTINGS_MODULE"] = module
    with redirect_stdout(sys.stderr):
        django = import_module("django")
        connections = import_module("django.db").connections
        connections.settings = connections.configure_settings(
            {"default": request["database"]}
        )
        try:
            settings = import_module("django.conf").settings
            if set(settings.DATABASES) != {"default"} or settings.DATABASE_ROUTERS:
                raise RuntimeError(
                    "SQL inspection requires one default database without routers"
                )
            settings.DATABASES = connections.settings
            django.setup()
            connection = connections["default"]
            if connection.vendor != "postgresql":
                raise ValueError("SQL inspection requires PostgreSQL")
            result = collect_sql_records(connection, keys)
        finally:
            connections.close_all()
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
