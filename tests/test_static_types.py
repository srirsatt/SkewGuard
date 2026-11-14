"""Validate ordered migration metadata without importing Django or using a database."""

import pytest
from pydantic import ValidationError

from skewguard.static.types import (
    AnalysisIssue,
    MigrationInventory,
    MigrationKey,
    MigrationOperation,
    MigrationRecord,
    SourceLocation,
)


def test_inventory_preserves_nested_metadata_through_json() -> None:
    initial = MigrationKey(app_label="catalog", name="0001_initial")
    rename = MigrationKey(app_label="catalog", name="0002_change")
    location = SourceLocation(path="catalog/migrations/0002_change.py", line=9)
    inventory = MigrationInventory(
        migrations=[
            MigrationRecord(key=initial, dependencies=[], operations=[]),
            MigrationRecord(
                key=rename,
                dependencies=[initial],
                source=location,
                file_sha256="a" * 64,
                operations=[
                    MigrationOperation(
                        index=0,
                        kind="RenameField",
                        model_name="product",
                        old_name="price",
                        new_name="amount",
                        source=location,
                    ),
                    MigrationOperation(
                        index=1,
                        kind="AlterModelOptions",
                        model_name="product",
                        details={"ordering": ["amount", "pk"]},
                    ),
                ],
            ),
        ]
    )

    restored = MigrationInventory.model_validate_json(inventory.model_dump_json())

    assert restored == inventory
    assert restored.migrations[1].operations[0].old_name == "price"
    assert restored.migrations[1].operations[1].details == {
        "ordering": ["amount", "pk"]
    }
    assert restored.migrations[1].file_sha256 == "a" * 64


def test_same_migration_name_in_different_apps_has_distinct_identity() -> None:
    authors = MigrationKey(app_label="authors", name="0001_initial")
    catalog = MigrationKey(app_label="catalog", name="0001_initial")
    inventory = MigrationInventory(
        migrations=[
            MigrationRecord(key=authors, dependencies=[], operations=[]),
            MigrationRecord(key=catalog, dependencies=[authors], operations=[]),
        ]
    )

    assert len({authors, catalog}) == 2
    assert [record.key for record in inventory.migrations] == [authors, catalog]


@pytest.mark.parametrize("indices", [[1], [0, 0], [1, 0], [0, 2]])
def test_record_rejects_incomplete_or_ambiguous_operation_order(
    indices: list[int],
) -> None:
    with pytest.raises(ValidationError, match="operation indices"):
        MigrationRecord(
            key=MigrationKey(app_label="catalog", name="0002_change"),
            dependencies=[],
            operations=[
                MigrationOperation(index=index, kind="AddField") for index in indices
            ],
        )


def test_empty_merge_migration_and_replacement_metadata_are_retained() -> None:
    old = MigrationKey(app_label="catalog", name="0001_initial")
    replacement = MigrationRecord(
        key=MigrationKey(app_label="catalog", name="0001_squashed_0003"),
        dependencies=[],
        operations=[],
        replaces=[old],
    )

    assert replacement.replaces == [old]
    assert replacement.operations == []


def test_inventory_rejects_duplicate_keys() -> None:
    record = MigrationRecord(
        key=MigrationKey(app_label="catalog", name="0001_initial"),
        dependencies=[],
        operations=[],
    )

    with pytest.raises(ValidationError, match="duplicate migration"):
        MigrationInventory(migrations=[record, record])


@pytest.mark.parametrize("include_parent", [False, True])
def test_inventory_rejects_missing_or_late_dependencies(include_parent: bool) -> None:
    parent = MigrationRecord(
        key=MigrationKey(app_label="authors", name="0001_initial"),
        dependencies=[],
        operations=[],
    )
    dependent = MigrationRecord(
        key=MigrationKey(app_label="catalog", name="0001_initial"),
        dependencies=[parent.key],
        operations=[],
    )

    with pytest.raises(ValidationError, match="dependencies must precede"):
        MigrationInventory(
            migrations=[dependent, parent] if include_parent else [dependent]
        )


def test_custom_operations_and_missing_source_remain_visible_without_verdicts() -> None:
    key = MigrationKey(app_label="catalog", name="0002_custom")
    inventory = MigrationInventory(
        migrations=[
            MigrationRecord(
                key=key,
                dependencies=[],
                operations=[MigrationOperation(index=0, kind="CustomBackfill")],
            )
        ],
        issues=[
            AnalysisIssue(
                kind="unsupported_operation",
                message="CustomBackfill requires inspection.",
                migration=key,
                operation_index=0,
            )
        ],
    )

    assert inventory.migrations[0].source is None
    assert inventory.migrations[0].operations[0].kind == "CustomBackfill"
    assert inventory.issues[0].kind == "unsupported_operation"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MigrationInventory.model_validate(
            {**inventory.model_dump(), "breaks_deploy_window": True}
        )


def test_operation_details_require_json_compatible_metadata() -> None:
    with pytest.raises(ValidationError):
        MigrationOperation.model_validate(
            {"index": 0, "kind": "AddField", "details": {"field": object()}}
        )


def test_issue_operation_index_requires_its_migration_identity() -> None:
    with pytest.raises(ValidationError, match="requires its migration identity"):
        AnalysisIssue(
            kind="unsupported_operation",
            message="Unknown operation.",
            operation_index=0,
        )


@pytest.mark.parametrize("line", [0, -1, True, "9"])
def test_source_lines_require_positive_integers(line: object) -> None:
    with pytest.raises(ValidationError):
        SourceLocation.model_validate(
            {"path": "catalog/migrations/0002_change.py", "line": line}
        )
