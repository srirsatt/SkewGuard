"""Compare actual release inventories using their isolated Django interpreters."""

import pytest

from skewguard.config import RunConfig
from skewguard.exec.envs import PythonEnvironment
from skewguard.static.migrations import analyze_migrations
from skewguard.static.types import MigrationAnalysis


@pytest.mark.parametrize("release", ["rename", "nullable", "required"])
def test_analyze_shop_releases_preserves_the_new_operation_metadata(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
    release: str,
) -> None:
    config, environments = shop_releases

    result = analyze_migrations(
        config,
        environments["base"],
        environments[release],
        settings_module="shop.settings",
    )

    assert result.issues == []
    assert len(result.base.migrations) == 1
    assert len(result.head.migrations) == 2
    assert len(result.new_migrations) == 1
    migration = result.new_migrations[0]
    assert migration.key.app_label == "catalog"
    assert migration.key.name == "0002_change"
    assert migration.dependencies == [result.base.migrations[0].key]
    operation = migration.operations[0]
    if release == "rename":
        assert operation.kind == "RenameField"
        assert operation.old_name == "price" and operation.new_name == "amount"
    else:
        assert operation.kind == "AddField"
        assert operation.field_name == (
            "notes" if release == "nullable" else "category"
        )
    assert MigrationAnalysis.model_validate_json(result.model_dump_json()) == result
