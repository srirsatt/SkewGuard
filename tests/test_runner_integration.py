"""Run checkout migrations in real Django environments against PostgreSQL."""

import os
from pathlib import Path

import pytest

from skewguard.config import RunConfig
from skewguard.exec.db import clone_database, connect_database, managed_postgres
from skewguard.exec.envs import (
    PythonEnvironment,
    create_environment,
    install_dependencies,
)
from skewguard.exec.runner import MigrationError, run_migrations

pytestmark = pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 to run tests requiring Docker",
)


def write_project(checkout: Path, version: str, *, renamed: bool) -> None:
    """Prepare a minimal release with deliberately unusable original DB settings."""
    field = "amount" if renamed else "price"
    files = {
        "requirements.txt": f"Django=={version}\npsycopg[binary]==3.3.7\n",
        "project/__init__.py": "",
        "project/settings.py": (
            'SECRET_KEY = "fixture-only"\n'
            'INSTALLED_APPS = ["products"]\n'
            'DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"\n'
            'DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", '
            '"NAME": "unreachable-original", "HOST": "127.0.0.1", "PORT": 1}}\n'
        ),
        "products/__init__.py": "",
        "products/models.py": (
            "from django.db import models\n"
            "class Product(models.Model):\n"
            f"    {field} = models.IntegerField()\n"
        ),
        "products/migrations/__init__.py": "",
        "products/migrations/0001_initial.py": (
            "from django.db import migrations, models\n"
            "class Migration(migrations.Migration):\n"
            "    initial = True\n"
            "    dependencies = []\n"
            '    operations = [migrations.CreateModel(name="Product", fields=['
            '("id", models.BigAutoField(primary_key=True)), '
            '("price", models.IntegerField())])]\n'
        ),
    }
    if renamed:
        files["products/migrations/0002_rename.py"] = (
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            '    dependencies = [("products", "0001_initial")]\n'
            "    operations = [migrations.RenameField("
            'model_name="product", old_name="price", new_name="amount")]\n'
        )
    for relative, contents in files.items():
        path = checkout / "backend" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)


@pytest.fixture(scope="module", params=["4.2.30", "5.2.19"])
def releases(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[RunConfig, PythonEnvironment, PythonEnvironment]:
    root = tmp_path_factory.mktemp("migration-releases")
    config = RunConfig(
        repo_path=root,
        base_ref="main",
        head_ref="HEAD",
        backend_path=Path("backend"),
    )
    version = str(request.param)
    base = root / "base"
    head = root / "head"
    write_project(base, version, renamed=False)
    write_project(head, version, renamed=True)
    base_env = create_environment(config, base)
    head_env = create_environment(config, head)
    install_dependencies(config, base_env)
    install_dependencies(config, head_env)
    return config, base_env, head_env


def test_base_and_head_migrate_separate_databases_and_can_repeat(
    releases: tuple[RunConfig, PythonEnvironment, PythonEnvironment],
) -> None:
    config, base, head = releases
    with managed_postgres(config) as server:
        baseline = clone_database(config, server)
        output = run_migrations(
            config, base, baseline, settings_module="project.settings"
        )
        assert "products.0001_initial... OK" in output
        with connect_database(config, baseline) as connection:
            connection.execute("INSERT INTO products_product (price) VALUES (20)")

        repeated = run_migrations(
            config, base, baseline, settings_module="project.settings"
        )
        assert "No migrations to apply" in repeated

        migrated = clone_database(config, server, source=baseline.dbname)
        output = run_migrations(
            config, head, migrated, settings_module="project.settings"
        )
        assert "products.0002_rename... OK" in output
        with connect_database(config, migrated) as connection:
            assert connection.execute(
                "SELECT amount FROM products_product"
            ).fetchall() == [(20,)]
        with connect_database(config, baseline) as connection:
            assert connection.execute(
                "SELECT price FROM products_product"
            ).fetchall() == [(20,)]

        # A successful clone proves the migration process left no source session.
        clone_database(config, server, source=migrated.dbname)


def test_missing_settings_module_reports_child_failure(
    releases: tuple[RunConfig, PythonEnvironment, PythonEnvironment],
) -> None:
    config, base, _ = releases
    with managed_postgres(config) as server:
        database = clone_database(config, server)
        with pytest.raises(MigrationError, match="ModuleNotFoundError") as raised:
            run_migrations(config, base, database, settings_module="missing.settings")
        assert database.password.get_secret_value() not in str(raised.value)


def test_a_slow_migration_is_stopped_at_the_subprocess_timeout(
    releases: tuple[RunConfig, PythonEnvironment, PythonEnvironment],
) -> None:
    config, _, head = releases
    migration = head.checkout / "backend/products/migrations/0003_slow.py"
    migration.write_text(
        "from django.db import migrations\n"
        "import time\n"
        "def wait(apps, schema_editor):\n"
        "    time.sleep(10)\n"
        "class Migration(migrations.Migration):\n"
        '    dependencies = [("products", "0002_rename")]\n'
        "    operations = [migrations.RunPython(wait)]\n"
    )
    try:
        with managed_postgres(config) as server:
            database = clone_database(config, server)
            limited = config.model_copy(update={"timeout_seconds": 1})
            with pytest.raises(MigrationError, match="timed out after 1s"):
                run_migrations(
                    limited, head, database, settings_module="project.settings"
                )
    finally:
        migration.unlink()
