"""Inspect real Django migration graphs without PostgreSQL or migration execution."""

import json
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from skewguard.config import RunConfig
from skewguard.exec.envs import (
    PythonEnvironment,
    create_environment,
    install_dependencies,
)
from skewguard.exec.runner import migration_environment
from skewguard.static.migrations import inspect_migrations
from skewguard.static.types import MigrationInventory, MigrationKey

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "skewguard/static/_migration_worker.py"


@pytest.fixture(scope="module", params=["4.2.30", "5.2.19"])
def worker_environment(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> PythonEnvironment:
    checkout = tmp_path_factory.mktemp("metadata-release")
    (checkout / "requirements.txt").write_text(
        f"Django=={request.param}\n", encoding="utf-8"
    )
    config = RunConfig(repo_path=checkout, base_ref="base", head_ref="head")
    environment = create_environment(config, checkout)
    install_dependencies(config, environment)
    return environment


@pytest.fixture
def project(
    worker_environment: PythonEnvironment, tmp_path: Path
) -> tuple[PythonEnvironment, Path]:
    backend = worker_environment.checkout / tmp_path.name
    shutil.copytree(
        ROOT / "benchmark/apps/shop",
        backend,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".skewguard-venv"),
    )
    with (backend / "shop/settings.py").open("a", encoding="utf-8") as settings:
        settings.write(
            '\nDATABASES = {"default": {"ENGINE": "original_database_must_not_load"}}\n'
        )
    return worker_environment, backend


def run_worker(
    project: tuple[PythonEnvironment, Path],
) -> subprocess.CompletedProcess[str]:
    environment, backend = project
    return subprocess.run(
        [
            str(environment.python),
            "-I",
            str(WORKER),
            str(backend),
            str(environment.checkout),
        ],
        cwd=backend,
        env=migration_environment(),
        input=json.dumps({"settings_module": "shop.settings"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )


def inventory(project: tuple[PythonEnvironment, Path]) -> MigrationInventory:
    result = run_worker(project)
    assert result.returncode == 0, result.stderr
    return MigrationInventory.model_validate_json(result.stdout)


def test_parent_launcher_validates_real_worker_output(
    project: tuple[PythonEnvironment, Path],
) -> None:
    environment, backend = project
    config = RunConfig(
        repo_path=environment.checkout,
        base_ref="base",
        head_ref="head",
        backend_path=backend.relative_to(environment.checkout),
    )

    result = inspect_migrations(config, environment, settings_module="shop.settings")

    assert result == inventory(project)
    assert result.migrations[0].key.app_label == "catalog"


def write_migration(backend: Path, name: str, source: str) -> Path:
    path = backend / "catalog/migrations" / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_inventory_captures_real_source_hash_and_model_fields(
    project: tuple[PythonEnvironment, Path],
) -> None:
    environment, backend = project
    result = inventory(project)
    record = result.migrations[0]

    assert record.key == MigrationKey(app_label="catalog", name="0001_initial")
    assert record.dependencies == []
    assert result.issues == []
    source = backend / "catalog/migrations/0001_initial.py"
    assert record.file_sha256 == sha256(source.read_bytes()).hexdigest()
    assert record.source is not None
    assert record.source.path == source.relative_to(environment.checkout).as_posix()
    assert record.source.line == 4
    operation = record.operations[0]
    assert operation.kind == "CreateModel" and operation.model_name == "Product"
    fields = operation.details["fields"]
    assert isinstance(fields, list)
    assert any(isinstance(field, dict) and field["name"] == "price" for field in fields)
    assert operation.details["options"] == {"ordering": ["price", "pk"]}


@pytest.mark.parametrize(
    ("case", "expected_kind", "field_name"),
    [
        ("shop_rename_price", "RenameField", None),
        ("shop_add_notes", "AddField", "notes"),
        ("shop_add_required_category", "AddField", "category"),
    ],
)
def test_shop_changes_preserve_rename_nullability_and_default_facts(
    project: tuple[PythonEnvironment, Path],
    case: str,
    expected_kind: str,
    field_name: str | None,
) -> None:
    _, backend = project
    shutil.copyfile(
        ROOT / "benchmark/cases" / f"{case}.py",
        backend / "catalog/migrations/0002_change.py",
    )

    result = inventory(project)

    assert result.issues == []
    assert len(result.migrations) == 2
    record = result.migrations[1]
    assert record.dependencies == [result.migrations[0].key]
    operation = record.operations[0]
    assert operation.kind == expected_kind
    assert operation.model_name == "product"
    assert operation.field_name == field_name
    if field_name is None:
        assert operation.old_name == "price" and operation.new_name == "amount"
        assert record.operations[1].details["options"] == {"ordering": ["amount", "pk"]}
    else:
        field = operation.details["field"]
        assert isinstance(field, dict)
        assert field["null"] is (field_name == "notes")
        assert field["has_default"] is (field_name == "category")
        assert field["has_db_default"] is False
        if field_name == "category":
            assert field["default_value"] == "general"


def test_dependency_order_follows_graph_across_apps_not_filenames(
    project: tuple[PythonEnvironment, Path],
) -> None:
    _, backend = project
    writers = backend / "writers/migrations"
    writers.mkdir(parents=True)
    (writers.parent / "__init__.py").write_text("", encoding="utf-8")
    (writers / "__init__.py").write_text("", encoding="utf-8")
    (writers / "0001_initial.py").write_text(
        "from django.db import migrations\nclass Migration(migrations.Migration):\n"
        "    dependencies = []\n    operations = []\n",
        encoding="utf-8",
    )
    with (backend / "shop/settings.py").open("a", encoding="utf-8") as settings:
        settings.write('\nINSTALLED_APPS = ["catalog", "writers"]\n')
    write_migration(
        backend,
        "0000_depends_on_writer",
        """from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial"), ("writers", "__first__")]
    operations = []
""",
    )

    result = inventory(project)

    keys = [record.key for record in result.migrations]
    writers_key = MigrationKey(app_label="writers", name="0001_initial")
    dependent = MigrationKey(app_label="catalog", name="0000_depends_on_writer")
    assert keys.index(writers_key) < keys.index(dependent)
    assert result.migrations[keys.index(dependent)].dependencies == [
        MigrationKey(app_label="catalog", name="0001_initial"),
        writers_key,
    ]


def test_custom_operation_code_data_migrations_and_defaults_are_not_called(
    project: tuple[PythonEnvironment, Path],
) -> None:
    _, backend = project
    marker = backend / "must-not-exist.txt"
    write_migration(
        backend,
        "0002_opaque",
        f"""from pathlib import Path
from django.db import migrations, models
from django.db.migrations.operations.base import Operation
print("migration import noise")
def default_value():
    raise AssertionError("default must not be called")
def write_data(apps, schema_editor):
    Path({str(marker)!r}).write_text("migration executed")
class RenameField(Operation):
    def describe(self):
        raise AssertionError("custom describe must not be called")
    def state_forwards(self, *args):
        raise AssertionError("state changes must not run")
    def database_forwards(self, *args):
        raise AssertionError("database changes must not run")
class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="product", name="token",
            field=models.CharField(max_length=32, default=default_value),
        ),
        migrations.RunPython(write_data),
        RenameField(),
    ]
""",
    )

    process = run_worker(project)

    assert process.returncode == 0, process.stderr
    assert "migration import noise" in process.stderr
    result = MigrationInventory.model_validate_json(process.stdout)
    operations = result.migrations[-1].operations
    assert [operation.kind for operation in operations] == [
        "AddField",
        "RunPython",
        "RenameField",
    ]
    field = operations[0].details["field"]
    assert isinstance(field, dict) and field["default_is_callable"] is True
    assert "default_value" not in field
    assert [issue.operation_index for issue in result.issues] == [1, 2]
    assert all(issue.kind == "unsupported_operation" for issue in result.issues)
    assert not marker.exists()


def test_replacement_migration_is_retained_as_an_explicit_history_issue(
    project: tuple[PythonEnvironment, Path],
) -> None:
    _, backend = project
    original = (backend / "catalog/migrations/0001_initial.py").read_text(
        encoding="utf-8"
    )
    replacement = original.replace(
        "initial = True",
        'initial = True\n    replaces = [("catalog", "0001_initial")]',
    )
    write_migration(backend, "0001_squashed", replacement)

    result = inventory(project)

    assert [record.key.name for record in result.migrations] == ["0001_squashed"]
    assert result.migrations[0].replaces == [
        MigrationKey(app_label="catalog", name="0001_initial")
    ]
    assert [issue.kind for issue in result.issues] == ["replacement_migration"]


def test_broken_dependency_graph_fails_without_returning_empty_metadata(
    project: tuple[PythonEnvironment, Path],
) -> None:
    _, backend = project
    write_migration(
        backend,
        "0002_invalid",
        """from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("catalog", "missing_migration")]
    operations = []
""",
    )

    result = run_worker(project)

    assert result.returncode != 0
    assert "NodeNotFoundError" in result.stderr
    assert result.stdout == ""


def test_startup_database_queries_are_blocked_by_the_dummy_backend(
    project: tuple[PythonEnvironment, Path],
) -> None:
    _, backend = project
    (backend / "catalog/apps.py").write_text(
        """from django.apps import AppConfig
class CatalogConfig(AppConfig):
    name = "catalog"
    def ready(self):
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
""",
        encoding="utf-8",
    )

    result = run_worker(project)

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "original_database_must_not_load" not in result.stderr
    assert result.stdout == ""


def test_settings_import_queries_cannot_use_the_original_database(
    project: tuple[PythonEnvironment, Path],
) -> None:
    _, backend = project
    with (backend / "shop/settings.py").open("a", encoding="utf-8") as settings:
        settings.write("\nfrom django.db import connection\nconnection.cursor()\n")

    result = run_worker(project)

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "original_database_must_not_load" not in result.stderr
    assert result.stdout == ""
