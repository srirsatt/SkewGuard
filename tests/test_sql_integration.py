"""Collect real PostgreSQL migration SQL with correctly staged schema context."""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from skewguard.config import RunConfig
from skewguard.exec.db import (
    DatabaseConnection,
    clone_database,
    connect_database,
    managed_postgres,
)
from skewguard.exec.envs import (
    PythonEnvironment,
    create_environment,
    install_dependencies,
)
from skewguard.exec.runner import run_migrations
from skewguard.static.migrations import analyze_migrations
from skewguard.static.sql import collect_sql, run_sql_worker

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 to run tests requiring Docker",
)


@pytest.fixture(scope="module", params=["4.2.30", "5.2.19"])
def sql_environments(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[PythonEnvironment, PythonEnvironment]:
    root = tmp_path_factory.mktemp("sql-releases")
    config = RunConfig(repo_path=root, base_ref="base", head_ref="head")
    environments: list[PythonEnvironment] = []
    for release in ("base", "head"):
        checkout = root / release
        checkout.mkdir()
        (checkout / "requirements.txt").write_text(
            f"Django=={request.param}\npsycopg[binary]==3.3.7\n",
            encoding="utf-8",
        )
        environment = create_environment(config, checkout)
        install_dependencies(config, environment)
        environments.append(environment)
    return environments[0], environments[1]


@pytest.fixture(scope="module")
def server(
    sql_environments: tuple[PythonEnvironment, PythonEnvironment],
) -> Iterator[DatabaseConnection]:
    config = RunConfig(
        repo_path=sql_environments[0].checkout.parent, base_ref="base", head_ref="head"
    )
    with managed_postgres(config) as database:
        yield database


@pytest.fixture
def sql_project(
    sql_environments: tuple[PythonEnvironment, PythonEnvironment],
    tmp_path: Path,
) -> tuple[RunConfig, PythonEnvironment, PythonEnvironment]:
    base, head = sql_environments
    backend_path = Path(tmp_path.name)
    for environment in (base, head):
        shutil.copytree(
            ROOT / "benchmark/apps/shop",
            environment.checkout / backend_path,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".skewguard-venv"),
        )
    config = RunConfig(
        repo_path=base.checkout.parent,
        base_ref="base",
        head_ref="head",
        backend_path=backend_path,
    )
    return config, base, head


def write_head(
    head: PythonEnvironment, config: RunConfig, name: str, source: str
) -> None:
    path = head.checkout / config.backend_path / "catalog/migrations" / f"{name}.py"
    path.write_text(source, encoding="utf-8")


@pytest.mark.parametrize(
    "case", ["shop_rename_price", "shop_add_notes", "shop_add_required_category"]
)
def test_sql_orchestration_captures_actual_shop_migration_sql(
    sql_project: tuple[RunConfig, PythonEnvironment, PythonEnvironment],
    server: DatabaseConnection,
    case: str,
) -> None:
    config, base, head = sql_project
    target = head.checkout / config.backend_path / "catalog/migrations/0002_change.py"
    shutil.copyfile(ROOT / "benchmark/cases" / f"{case}.py", target)
    metadata = analyze_migrations(config, base, head, settings_module="shop.settings")

    result = collect_sql(
        config, base, head, server, metadata, settings_module="shop.settings"
    )

    assert len(result.results) == 1
    item = result.results[0]
    assert item.status == "collected", item.detail
    assert item.sql is not None
    if case == "shop_rename_price":
        assert 'RENAME COLUMN "price" TO "amount"' in item.sql
    elif case == "shop_add_notes":
        assert 'ADD COLUMN "notes" text NULL' in item.sql
    else:
        assert 'ADD COLUMN "category"' in item.sql
        assert "DEFAULT 'general'" in item.sql
        assert "DROP DEFAULT" in item.sql


def test_later_sql_uses_constraints_created_on_inspection_copy_only(
    sql_project: tuple[RunConfig, PythonEnvironment, PythonEnvironment],
    server: DatabaseConnection,
) -> None:
    config, base, head = sql_project
    for name, parent, unique in (
        ("0002_unique", "0001_initial", True),
        ("0003_remove_unique", "0002_unique", False),
    ):
        write_head(
            head,
            config,
            name,
            f'''from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = [("catalog", "{parent}")]
    operations = [migrations.AlterField(
        model_name="product", name="price", field=models.IntegerField(unique={unique}),
    )]
''',
        )
    metadata = analyze_migrations(config, base, head, settings_module="shop.settings")
    original = clone_database(config, server)
    run_migrations(config, base, original, settings_module="shop.settings")
    inspection = clone_database(config, server, source=original.dbname)

    result = run_sql_worker(
        config,
        head,
        inspection,
        metadata.new_migrations,
        settings_module="shop.settings",
    )

    assert [item.status for item in result.results] == ["collected", "collected"]
    assert "UNIQUE" in (result.results[0].sql or "")
    assert "DROP CONSTRAINT" in (result.results[1].sql or "")
    query = (
        "SELECT count(*) FROM pg_constraint "
        "WHERE conrelid = 'catalog_product'::regclass AND contype = 'u' "
        "AND pg_get_constraintdef(oid) LIKE '%price%'"
    )
    with connect_database(config, original) as connection:
        assert connection.execute(query).fetchone() == (0,)
    with connect_database(config, inspection) as connection:
        # Only the earlier migration is applied; the final SQL is collected.
        assert connection.execute(query).fetchone() == (1,)
    clone_database(config, server, source=inspection.dbname)


def test_python_data_migration_sql_is_partial_and_last_body_is_not_applied(
    sql_project: tuple[RunConfig, PythonEnvironment, PythonEnvironment],
    server: DatabaseConnection,
) -> None:
    config, base, head = sql_project
    marker = head.checkout / "should-not-exist.txt"
    write_head(
        head,
        config,
        "0002_python",
        f"""from pathlib import Path
from django.db import migrations
def write_rows(apps, schema_editor):
    Path({str(marker)!r}).write_text("data migration applied")
class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [migrations.RunPython(write_rows)]
""",
    )
    metadata = analyze_migrations(config, base, head, settings_module="shop.settings")

    result = collect_sql(
        config, base, head, server, metadata, settings_module="shop.settings"
    )

    assert result.results[0].status == "partial"
    assert "CANNOT BE WRITTEN AS SQL" in (result.results[0].sql or "")
    assert any(issue.kind == "sql_partial" for issue in result.issues)
    assert not marker.exists()


def test_failed_prefix_advancement_blocks_later_sql_without_a_break_verdict(
    sql_project: tuple[RunConfig, PythonEnvironment, PythonEnvironment],
    server: DatabaseConnection,
) -> None:
    config, base, head = sql_project
    write_head(
        head,
        config,
        "0002_fails",
        """from django.db import migrations
def fail(apps, schema_editor):
    raise ValueError("expected inspection advancement failure")
class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [migrations.RunPython(fail)]
""",
    )
    write_head(
        head,
        config,
        "0003_later",
        """from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = [("catalog", "0002_fails")]
    operations = [migrations.AddField(
        model_name="product", name="notes", field=models.TextField(null=True),
    )]
""",
    )
    metadata = analyze_migrations(config, base, head, settings_module="shop.settings")

    result = collect_sql(
        config, base, head, server, metadata, settings_module="shop.settings"
    )

    assert [item.status for item in result.results] == ["partial", "unavailable"]
    assert any(issue.kind == "sql_state_unavailable" for issue in result.issues)
    assert "expected inspection advancement failure" in (result.results[1].detail or "")


def test_schema_plan_mismatch_returns_unavailable_sql_without_advancing(
    sql_project: tuple[RunConfig, PythonEnvironment, PythonEnvironment],
    server: DatabaseConnection,
) -> None:
    config, base, head = sql_project
    shutil.copyfile(
        ROOT / "benchmark/cases/shop_add_notes.py",
        head.checkout / config.backend_path / "catalog/migrations/0002_change.py",
    )
    metadata = analyze_migrations(config, base, head, settings_module="shop.settings")
    empty = clone_database(config, server)

    result = run_sql_worker(
        config, head, empty, metadata.new_migrations, settings_module="shop.settings"
    )

    assert result.results[0].status == "unavailable"
    assert result.issues[0].kind == "sql_state_unavailable"
    with connect_database(config, empty) as connection:
        assert connection.execute(
            "SELECT to_regclass('catalog_product')"
        ).fetchone() == (None,)


def test_custom_sql_hooks_are_not_executed_for_collection(
    sql_project: tuple[RunConfig, PythonEnvironment, PythonEnvironment],
    server: DatabaseConnection,
) -> None:
    config, base, head = sql_project
    write_head(
        head,
        config,
        "0002_custom",
        """from django.db import migrations
from django.db.migrations.operations.base import Operation
class CustomSQL(Operation):
    reduces_to_sql = True
    def state_forwards(self, *args):
        raise AssertionError("custom state must not be evaluated for SQL collection")
    def database_forwards(self, *args):
        raise AssertionError("custom SQL hook must not execute for collection")
class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [CustomSQL()]
""",
    )
    metadata = analyze_migrations(config, base, head, settings_module="shop.settings")

    result = collect_sql(
        config, base, head, server, metadata, settings_module="shop.settings"
    )

    assert result.results[0].status == "unavailable"
    assert "Custom operation" in (result.results[0].detail or "")


def test_callable_defaults_are_labeled_as_partial_sql(
    sql_project: tuple[RunConfig, PythonEnvironment, PythonEnvironment],
    server: DatabaseConnection,
) -> None:
    config, base, head = sql_project
    write_head(
        head,
        config,
        "0002_callable",
        """from django.db import migrations, models
def value():
    return "generated"
class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [migrations.AddField(
        model_name="product", name="token",
        field=models.CharField(max_length=32, default=value),
    )]
""",
    )
    metadata = analyze_migrations(config, base, head, settings_module="shop.settings")

    result = collect_sql(
        config, base, head, server, metadata, settings_module="shop.settings"
    )

    assert result.results[0].status == "partial"
    assert "generated" in (result.results[0].sql or "")
    assert "Callable defaults" in (result.results[0].detail or "")
