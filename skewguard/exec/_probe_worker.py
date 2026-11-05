"""Django-side probe worker; depends only on the selected release's packages."""

import json
import os
import sys
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from importlib import import_module
from uuid import UUID


class ProbeValueError(ValueError):
    """The deterministic generator cannot supply this model's required inputs."""


def build_create_values(model_label: str) -> dict[str, object]:
    """Prepare common field values; preserve model defaults and use seeded FKs.

    Unsupported types or exhausted unique choices raise ProbeValueError. That
    indicates unavailable probe input, not evidence of a deploy regression.
    """
    model = import_module("django.apps").apps.get_model(model_label)
    values: dict[str, object] = {}
    for field in model._meta.concrete_fields:
        kind = field.get_internal_type()
        if kind in {"AutoField", "BigAutoField", "SmallAutoField"} or getattr(
            field, "generated", False
        ):
            continue
        if field.has_default() or getattr(field, "has_db_default", lambda: False)():
            continue
        if field.null:
            values[field.attname] = None
            continue

        candidates: list[object]
        if field.choices:
            candidates = [value for value, _ in field.flatchoices if value is not None]
        elif field.is_relation:
            related = field.related_model
            if related is None:
                raise ProbeValueError(f"unresolved relationship: {field.name}")
            candidates = list(
                related._default_manager.using("default")
                .order_by(related._meta.pk.attname)
                .values_list(field.target_field.attname, flat=True)[:32]
            )
        else:
            candidates = [
                _scalar_value(model_label, field.name, kind, field, attempt)
                for attempt in range(32)
            ]

        for value in candidates:
            if (
                not field.unique
                or not model._default_manager.using("default")
                .filter(**{field.attname: value})
                .exists()
            ):
                values[field.attname] = value
                break
        else:
            raise ProbeValueError(f"no available value for {model_label}.{field.name}")
    return values


def _scalar_value(
    model_label: str, name: str, kind: str, field: object, attempt: int
) -> object:
    """Return deterministic candidates for built-in scalar field types."""
    digest = sha256(f"{model_label}.{name}:{attempt}".encode()).hexdigest()
    maximum = getattr(field, "max_length", None)
    if kind in {"CharField", "TextField", "SlugField", "EmailField"}:
        value = f"probe{digest[:8]}@example.test" if kind == "EmailField" else digest
        return value[:maximum] if maximum is not None else value
    if kind in {
        "IntegerField",
        "BigIntegerField",
        "SmallIntegerField",
        "PositiveIntegerField",
        "PositiveSmallIntegerField",
        "PositiveBigIntegerField",
    }:
        return attempt + 1
    if kind == "BooleanField":
        return bool(attempt % 2)
    if kind == "FloatField":
        return float(attempt + 1)
    if kind == "DecimalField":
        return Decimal(attempt + 1).scaleb(-getattr(field, "decimal_places", 0))
    if kind == "UUIDField":
        return UUID(digest[:32], version=4)
    if kind == "DateField":
        return date(2024, 1, 1) + timedelta(days=attempt)
    if kind == "DateTimeField":
        return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(seconds=attempt)
    if kind == "TimeField":
        return (datetime(2024, 1, 1) + timedelta(seconds=attempt)).time()
    if kind == "DurationField":
        return timedelta(seconds=attempt + 1)
    if kind == "JSONField":
        return {"skewguard": attempt}
    raise ProbeValueError(f"unsupported field type: {model_label}.{name} ({kind})")


def probe_model(model_label: str, row_limit: int) -> dict[str, object]:
    """Read bounded rows and every concrete field using an initialized Django app.

    Return an Outcome-shaped mapping with scenario_id='orm:<label>:read'. On
    success, body contains the row dictionaries. On query failure, record the
    exception type/message and whether it is a Django IntegrityError. Reject
    nonpositive row limits. Preserve the model's declared default ordering.
    """
    if row_limit <= 0:
        raise ValueError("row_limit must be positive")

    scenario_id = f"orm:{model_label}:read"

    try:
        model = import_module("django.apps").apps.get_model(model_label)
        fields = [field.attname for field in model._meta.concrete_fields]

        rows: list[dict[str, object]] = []
        for instance in model._default_manager.all()[:row_limit]:
            rows.append({name: getattr(instance, name) for name in fields})

        return {
            "scenario_id": scenario_id,
            "status": "ok",
            "body": rows,
        }

    except Exception as error:
        integrity_error = import_module("django.db").IntegrityError
        return {
            "scenario_id": scenario_id,
            "status": "exception",
            "exception_type": type(error).__name__,
            "exception_message": str(error),
            "integrity_error": isinstance(error, integrity_error),
        }


def probe_model_write(model_label: str) -> dict[str, object]:
    """Attempt one insert and record its result after rolling back the transaction.

    """
    scenario_id = f"orm:{model_label}:write"

    try:
        model = import_module("django.apps").apps.get_model(model_label)
        transaction = import_module("django.db.transaction")

        with transaction.atomic(using="default"):
            values = build_create_values(model_label)
            model._default_manager.db_manager("default").create(**values)
            transaction.set_rollback(True, using="default")

        return {
            "scenario_id": scenario_id,
            "status": "ok",
            "body": {"created": True, "rolled_back": True},
        }

    except Exception as error:
        integrity_error = import_module("django.db").IntegrityError
        return {
            "scenario_id": scenario_id,
            "status": "exception",
            "exception_type": type(error).__name__,
            "exception_message": str(error),
            "integrity_error": isinstance(error, integrity_error),
        }


def commit_model_writes(model_labels: list[str]) -> list[dict[str, object]]:
    """Commit new-code inserts together to prepare rollback checks.

    Any failure aborts the batch. Return success evidence only after the outer
    transaction commits; ordinary write probes continue to roll back their rows.
    """
    transaction = import_module("django.db.transaction")
    outcomes: list[dict[str, object]] = []
    with transaction.atomic(using="default"):
        for label in model_labels:
            model = import_module("django.apps").apps.get_model(label)
            values = build_create_values(label)
            model._default_manager.db_manager("default").create(**values)
            outcomes.append(
                {
                    "scenario_id": f"orm:{label}:commit",
                    "status": "ok",
                    "body": {"created": True, "rolled_back": False},
                }
            )
    return outcomes


def main() -> None:
    request = json.load(sys.stdin)
    sys.path.insert(0, sys.argv[1])
    os.environ["DJANGO_SETTINGS_MODULE"] = request["settings_module"]
    django = import_module("django")
    settings = import_module("django.conf").settings
    connections = import_module("django.db").connections
    if set(settings.DATABASES) != {"default"} or settings.DATABASE_ROUTERS:
        raise RuntimeError("Probe worker requires one default database without routers")
    settings.DATABASES = {"default": request["database"]}

    try:
        with redirect_stdout(sys.stderr):
            django.setup()
            if request["operation"] == "seed":
                import_module("django.core.management").call_command(
                    "loaddata", request["fixture"], database="default", verbosity=0
                )
                outcomes = []
            else:
                if request["operation"] not in {"read", "write", "commit"}:
                    raise ValueError("unknown probe worker operation")
                apps = import_module("django.apps").apps
                labels = request["model_labels"]
                if labels is None:
                    labels = sorted(model._meta.label for model in apps.get_models())
                if request["operation"] == "read":
                    outcomes = [
                        probe_model(label, request["row_limit"]) for label in labels
                    ]
                elif request["operation"] == "write":
                    outcomes = [probe_model_write(label) for label in labels]
                else:
                    outcomes = commit_model_writes(labels)
    finally:
        connections.close_all()

    print(json.dumps(outcomes, default=str))


if __name__ == "__main__":
    main()
