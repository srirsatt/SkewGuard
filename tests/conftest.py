"""Shared prepared application releases for Postgres integration tests."""

import shutil
from pathlib import Path

import pytest

from skewguard.config import RunConfig
from skewguard.exec.envs import (
    PythonEnvironment,
    create_environment,
    install_dependencies,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def shop_releases(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[RunConfig, dict[str, PythonEnvironment]]:
    root = tmp_path_factory.mktemp("shop-releases")
    config = RunConfig(repo_path=root, base_ref="base", head_ref="head")
    environments: dict[str, PythonEnvironment] = {}
    for release in ("base", "rename", "nullable", "required"):
        checkout = root / release
        shutil.copytree(
            ROOT / "benchmark/apps/shop",
            checkout,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".skewguard-venv"),
        )
        if release != "base":
            case = {
                "rename": "shop_rename_price",
                "nullable": "shop_add_notes",
                "required": "shop_add_required_category",
            }[release]
            shutil.copyfile(
                ROOT / "benchmark/cases" / f"{case}.py",
                checkout / "catalog/migrations/0002_change.py",
            )
            model = checkout / "catalog/models.py"
            if release == "rename":
                for path in (model, checkout / "catalog/views.py"):
                    path.write_text(path.read_text().replace("price", "amount"))
            elif release == "nullable":
                model.write_text(
                    model.read_text().replace(
                        "    class Meta:",
                        "    notes = models.TextField(null=True)\n\n    class Meta:",
                    )
                )
            else:
                model.write_text(
                    model.read_text().replace(
                        "    class Meta:",
                        "    category = models.CharField("
                        'max_length=32, default="general")'
                        "\n\n    class Meta:",
                    )
                )
        environment = create_environment(config, checkout)
        install_dependencies(config, environment)
        environments[release] = environment
    return config, environments
