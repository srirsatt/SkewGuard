"""Django-side metadata worker, using only the selected release's dependencies."""

import inspect
import json
import os
import sys
from contextlib import redirect_stdout
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any

_SUPPORTED_OPERATIONS = (
    "CreateModel",
    "DeleteModel",
    "RenameModel",
    "AlterModelTable",
    "AlterModelOptions",
    "AddField",
    "RemoveField",
    "AlterField",
    "RenameField",
)


def migration_key(key: tuple[str, str]) -> dict[str, str]:
    return {"app_label": key[0], "name": key[1]}


def json_literal(value: object) -> bool:
    """Check metadata serialization without evaluating callable defaults."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def qualified_type(value: object) -> str:
    kind = type(value)
    return f"{kind.__module__}.{kind.__qualname__}"


def field_metadata(field: object) -> dict[str, object]:
    """Record field options and default presence without invoking defaults."""
    not_provided = import_module("django.db.models").NOT_PROVIDED
    details: dict[str, object] = {"class_path": qualified_type(field)}
    for name in ("null", "unique", "primary_key", "max_length", "db_column"):
        value = getattr(field, name, None)
        if json_literal(value):
            details[name] = value
        else:
            details[f"{name}_class"] = qualified_type(value)
    for name in ("default", "db_default"):
        value = getattr(field, name, not_provided)
        provided = value is not not_provided
        details[f"has_{name}"] = provided
        if provided:
            is_callable = callable(value)
            details[f"{name}_is_callable"] = is_callable
            if not is_callable and json_literal(value):
                details[f"{name}_value"] = value
            else:
                details[f"{name}_class"] = qualified_type(value)
    return details


def migration_source(
    migration: object,
    checkout: Path,
) -> tuple[dict[str, object] | None, str | None]:
    """Capture real source coordinates and hash, leaving unavailable facts unset."""
    try:
        filename = inspect.getsourcefile(type(migration))
        if filename is None:
            return None, None
        path = Path(filename).resolve()
        digest = sha256(path.read_bytes()).hexdigest()
    except (OSError, TypeError):
        return None, None
    try:
        _, line = inspect.getsourcelines(type(migration))
    except (OSError, TypeError):
        line = None
    try:
        source_path = path.relative_to(checkout).as_posix()
    except ValueError:
        source_path = path.as_posix()
    return {"path": source_path, "line": line}, digest


def operation_metadata(
    operation: Any,
    index: int,
    key: tuple[str, str],
    issues: list[dict[str, object]],
) -> dict[str, object]:
    """Extract common built-in operations; retain opaque/custom operations as gaps."""
    kind = type(operation).__name__
    details: dict[str, object] = {"class_path": qualified_type(operation)}
    record: dict[str, object] = {"index": index, "kind": kind, "details": details}
    operations = import_module("django.db.migrations.operations")
    if kind not in _SUPPORTED_OPERATIONS or type(operation) is not getattr(
        operations, kind, None
    ):
        issues.append(
            {
                "kind": "unsupported_operation",
                "migration": migration_key(key),
                "operation_index": index,
                "message": (
                    "Metadata extraction is incomplete for "
                    f"{qualified_type(operation)}."
                ),
            }
        )
        return record

    record["description"] = operation.describe()
    model_name = getattr(operation, "model_name", None)
    if model_name is None:
        model_name = getattr(
            operation, "old_name" if kind == "RenameModel" else "name", None
        )
    record["model_name"] = model_name
    if kind in {"AddField", "RemoveField", "AlterField"}:
        record["field_name"] = operation.name
    if kind in {"RenameField", "RenameModel"}:
        record["old_name"] = operation.old_name
        record["new_name"] = operation.new_name
    if kind in {"AddField", "AlterField"}:
        details["field"] = field_metadata(operation.field)
        details["preserve_default"] = operation.preserve_default
    elif kind == "CreateModel":
        details["fields"] = [
            {"name": name, "field": field_metadata(field)}
            for name, field in operation.fields
        ]
    elif kind == "AlterModelTable":
        details["table"] = operation.table
    if kind in {"CreateModel", "AlterModelOptions"}:
        options = operation.options
        if json_literal(options):
            details["options"] = options
        else:
            issues.append(
                {
                    "kind": "unsupported_operation",
                    "migration": migration_key(key),
                    "operation_index": index,
                    "message": "Model options contain values not representable "
                    "as JSON.",
                }
            )
    return record


def collect_inventory(checkout: Path) -> dict[str, object]:
    """Read Django's active migration graph without applying state/database changes.

    Loader(None) treats migrations as unapplied and resolves replacement nodes.
    Such replacements are retained and flagged; their presence alone does not
    establish which migrations a base release would need to apply.
    """
    loader = import_module("django.db.migrations.loader").MigrationLoader(None)
    conflicts = loader.detect_conflicts()
    if conflicts:
        raise ValueError(f"Conflicting migration leaf nodes: {conflicts}")
    graph = loader.graph
    ordered: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for leaf in sorted(graph.leaf_nodes()):
        for key in graph.forwards_plan(leaf):
            if key not in seen:
                ordered.append(key)
                seen.add(key)

    records: list[dict[str, object]] = []
    issues: list[dict[str, object]] = []
    for key in ordered:
        migration = graph.nodes[key]
        source, digest = migration_source(migration, checkout)
        if source is None:
            issues.append(
                {
                    "kind": "source_unavailable",
                    "migration": migration_key(key),
                    "message": "Migration source coordinates and file hash "
                    "are unavailable.",
                }
            )
        replaces = [migration_key(item) for item in migration.replaces]
        if replaces:
            issues.append(
                {
                    "kind": "replacement_migration",
                    "migration": migration_key(key),
                    "source": source,
                    "message": "Replacement migration requires explicit "
                    "history comparison.",
                }
            )
        records.append(
            {
                "key": migration_key(key),
                "source": source,
                "file_sha256": digest,
                "dependencies": [
                    migration_key(parent)
                    for parent in sorted(
                        node.key for node in graph.node_map[key].parents
                    )
                ],
                "replaces": replaces,
                "operations": [
                    operation_metadata(operation, index, key, issues)
                    for index, operation in enumerate(migration.operations)
                ],
            }
        )
    return {"migrations": records, "issues": issues}


def main() -> None:
    request = json.load(sys.stdin)
    settings_module = request.get("settings_module")
    if (
        not isinstance(settings_module, str)
        or not settings_module
        or not all(part.isidentifier() for part in settings_module.split("."))
    ):
        raise ValueError("settings_module must be a dotted Python module name")
    backend = Path(sys.argv[1]).resolve()
    checkout = Path(sys.argv[2]).resolve()
    if not backend.is_dir() or not backend.is_relative_to(checkout):
        raise ValueError("backend must be a directory within the checkout")
    sys.path.insert(0, str(backend))
    sys.dont_write_bytecode = True
    os.environ["DJANGO_SETTINGS_MODULE"] = settings_module

    with redirect_stdout(sys.stderr):
        django = import_module("django")
        connections = import_module("django.db").connections
        connections.settings = connections.configure_settings(
            {"default": {"ENGINE": "django.db.backends.dummy"}}
        )
        try:
            settings = import_module("django.conf").settings
            settings.DATABASES = connections.settings
            settings.DATABASE_ROUTERS = []
            django.setup()
            inventory = collect_inventory(checkout)
        finally:
            connections.close_all()
    print(json.dumps(inventory, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
