"""Keep planning context complete and release-specific without static verdicts."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import SecretStr, ValidationError

from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import PythonEnvironment, environment_python
from skewguard.static import context
from skewguard.static.code_refs import find_references, reference_targets
from skewguard.static.context import collect_static_context
from skewguard.static.migrations import MigrationInspectionError, compare_inventories
from skewguard.static.types import (
    AnalysisIssue,
    CodeReferenceScan,
    MigrationAnalysis,
    MigrationInventory,
    MigrationKey,
    MigrationOperation,
    MigrationRecord,
    MigrationSQL,
    SQLExtraction,
    StaticContext,
)


def analysis() -> MigrationAnalysis:
    old = MigrationRecord(
        key=MigrationKey(app_label="catalog", name="0001_initial"),
        dependencies=[],
        operations=[],
        file_sha256="a" * 64,
    )
    new = MigrationRecord(
        key=MigrationKey(app_label="catalog", name="0002_change"),
        dependencies=[old.key],
        operations=[
            MigrationOperation(
                index=0,
                kind="RenameField",
                model_name="product",
                old_name="price",
                new_name="amount",
            )
        ],
        file_sha256="b" * 64,
    )
    return compare_inventories(
        MigrationInventory(migrations=[old]),
        MigrationInventory(migrations=[old, new]),
    )


def sql_result(metadata: MigrationAnalysis) -> SQLExtraction:
    return SQLExtraction(
        results=[
            MigrationSQL(
                migration=metadata.new_migrations[0].key,
                status="collected",
                sql='ALTER TABLE "catalog_product" RENAME COLUMN "price" TO "amount";',
            )
        ]
    )


def complete_context() -> StaticContext:
    metadata = analysis()
    targets = reference_targets(metadata)
    return StaticContext(
        migrations=metadata,
        sql=sql_result(metadata),
        base_code=find_references(
            'Product.objects.values("price")', "views.py", targets
        ),
        head_code=find_references(
            'Product.objects.values("amount")', "views.py", targets
        ),
    )


def test_context_roundtrips_with_separate_old_and_new_reference_evidence() -> None:
    result = complete_context()
    assert StaticContext.model_validate_json(result.model_dump_json()) == result
    assert {
        ref.field_name for ref in result.base_code.references if ref.field_name
    } == {"price"}
    assert {
        ref.field_name for ref in result.head_code.references if ref.field_name
    } == {"amount"}


@pytest.mark.parametrize("mode", ["missing", "foreign", "extra"])
def test_context_rejects_sql_from_a_different_migration_selection(mode: str) -> None:
    result = complete_context()
    records = list(result.sql.results)
    foreign = MigrationSQL(
        migration=MigrationKey(app_label="billing", name="0002_change"),
        status="collected",
        sql="",
    )
    records = (
        []
        if mode == "missing"
        else [foreign]
        if mode == "foreign"
        else [*records, foreign]
    )
    with pytest.raises(ValidationError, match="SQL results must match"):
        StaticContext(
            migrations=result.migrations,
            sql=SQLExtraction(results=records),
            base_code=result.base_code,
            head_code=result.head_code,
        )


@pytest.mark.parametrize("release", ["base_code", "head_code"])
def test_context_rejects_references_from_unselected_migrations(release: str) -> None:
    result = complete_context()
    reference = result.base_code.references[0]
    target = reference.target.model_copy(
        update={
            "migration": MigrationKey(app_label="billing", name="0002_change"),
        }
    )
    scan = CodeReferenceScan(
        references=[reference.model_copy(update={"target": target})],
        sql_literals=[],
        issues=[],
    )
    with pytest.raises(ValidationError, match="target selected migrations"):
        StaticContext.model_validate({**result.model_dump(), release: scan})


def test_unavailable_sql_and_release_gaps_remain_valid_planning_context() -> None:
    result = complete_context()
    sql_issue = AnalysisIssue(kind="sql_unavailable", message="No SQL available.")
    gap = AnalysisIssue(kind="code_parse_error", message="Cannot parse new view.")
    context_with_gaps = StaticContext(
        migrations=result.migrations,
        sql=SQLExtraction(
            results=[
                MigrationSQL(
                    migration=result.migrations.new_migrations[0].key,
                    status="unavailable",
                    detail="No SQL available.",
                )
            ],
            issues=[sql_issue],
        ),
        base_code=result.base_code,
        head_code=CodeReferenceScan(references=[], sql_literals=[], issues=[gap]),
    )
    assert context_with_gaps.sql.issues == [sql_issue]
    assert context_with_gaps.head_code.issues == [gap]
    assert context_with_gaps.base_code.references


@pytest.fixture
def releases(
    tmp_path: Path,
) -> tuple[RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection]:
    environments: list[PythonEnvironment] = []
    for label, field in (("base", "price"), ("head", "amount")):
        checkout = tmp_path / label
        backend = checkout / "backend"
        backend.mkdir(parents=True)
        (backend / "views.py").write_text(
            f'Product.objects.values("{field}")\n',
            encoding="utf-8",
        )
        venv = checkout / ".skewguard-venv"
        environments.append(
            PythonEnvironment(
                checkout=checkout,
                venv=venv,
                python=environment_python(venv),
            )
        )
    return (
        RunConfig(
            repo_path=tmp_path,
            base_ref="base",
            head_ref="head",
            backend_path=Path("backend"),
        ),
        environments[0],
        environments[1],
        DatabaseConnection(
            host="localhost",
            port=5432,
            username="test",
            password=SecretStr("sandbox-only"),
            dbname="postgres",
        ),
    )


@pytest.mark.parametrize("sql_available", [True, False])
def test_collector_keeps_old_and_new_sources_even_when_sql_is_unavailable(
    releases: tuple[
        RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection
    ],
    monkeypatch: pytest.MonkeyPatch,
    sql_available: bool,
) -> None:
    config, base, head, server = releases
    metadata = analysis()
    scripts = sql_result(metadata)
    if not sql_available:
        scripts = SQLExtraction(
            results=[
                MigrationSQL(
                    migration=metadata.new_migrations[0].key,
                    status="unavailable",
                    detail="Sandbox schema setup failed.",
                )
            ],
            issues=[
                AnalysisIssue(kind="sql_unavailable", message="Schema setup failed.")
            ],
        )
        (head.checkout / "backend/broken.py").write_text(
            "def broken(:", encoding="utf-8"
        )
    inspect = Mock(return_value=metadata)
    collect_sql = Mock(return_value=scripts)
    monkeypatch.setattr(context.migrations, "analyze_migrations", inspect)
    monkeypatch.setattr(context.sql, "collect_sql", collect_sql)

    result = collect_static_context(
        config, base, head, server, settings_module="shop.settings"
    )

    assert result.migrations == metadata
    assert result.sql == scripts
    assert {
        ref.field_name for ref in result.base_code.references if ref.field_name
    } == {"price"}
    assert {
        ref.field_name for ref in result.head_code.references if ref.field_name
    } == {"amount"}
    assert {ref.source.path for ref in result.base_code.references} == {
        "backend/views.py"
    }
    if not sql_available:
        assert result.head_code.issues[0].kind == "code_parse_error"
        assert not result.base_code.issues
    inspect.assert_called_once_with(config, base, head, settings_module="shop.settings")
    collect_sql.assert_called_once_with(
        config,
        base,
        head,
        server,
        metadata,
        settings_module="shop.settings",
    )


def test_empty_selection_preserves_history_issues_in_the_collected_context(
    releases: tuple[
        RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, base, head, server = releases
    original = analysis().base
    rewritten = original.model_copy(
        update={
            "migrations": [
                original.migrations[0].model_copy(update={"file_sha256": "c" * 64}),
            ]
        }
    )
    metadata = compare_inventories(original, rewritten)
    monkeypatch.setattr(
        context.migrations, "analyze_migrations", Mock(return_value=metadata)
    )
    # Real collect_sql must return immediately without trying to connect to server.
    result = collect_static_context(
        config, base, head, server, settings_module="shop.settings"
    )
    assert not result.migrations.new_migrations
    assert {issue.kind for issue in result.migrations.issues} == {"rewritten_migration"}
    assert result.sql.issues == metadata.issues
    assert result.base_code.issues == metadata.issues
    assert result.head_code.issues == metadata.issues
    assert not result.sql.results


def test_fatal_inspection_error_is_not_replaced_with_empty_context(
    releases: tuple[
        RunConfig, PythonEnvironment, PythonEnvironment, DatabaseConnection
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, base, head, server = releases
    error = MigrationInspectionError("Django migration graph could not be loaded.")
    monkeypatch.setattr(
        context.migrations, "analyze_migrations", Mock(side_effect=error)
    )
    collect_sql = Mock()
    monkeypatch.setattr(context.sql, "collect_sql", collect_sql)
    with pytest.raises(MigrationInspectionError) as raised:
        collect_static_context(
            config, base, head, server, settings_module="shop.settings"
        )
    assert raised.value is error
    collect_sql.assert_not_called()
