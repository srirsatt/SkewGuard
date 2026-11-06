"""Shared prepared application releases for Postgres integration tests."""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from skewguard.config import RunConfig
from skewguard.exec.diff import compare_matrix
from skewguard.exec.envs import (
    PythonEnvironment,
    create_environment,
    install_dependencies,
)
from skewguard.exec.types import (
    Cell,
    CellResult,
    ComparisonPolicy,
    Outcome,
    ReproductionAttempt,
    VerificationResult,
)
from skewguard.exec.verify import classify_reproductions
from skewguard.exec.worktree import CheckoutPair
from skewguard.report.json_report import RunReport, build_report

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


@pytest.fixture
def report_factory(tmp_path: Path) -> Callable[[str], RunReport]:
    """Build internally consistent execution evidence for report/interface tests."""
    read = "orm:catalog.Product:read"
    commit = "orm:catalog.Product:commit"

    def make_report(mode: str) -> RunReport:
        if mode not in {
            "safe",
            "breaking",
            "flaky",
            "inconclusive",
            "coverage",
            "head_failure",
            "replay_only",
            "hostile",
            "body_change",
        }:
            raise ValueError(f"unknown report fixture: {mode}")
        message = (
            "<script>alert(1)</script> | [link](https://example.invalid)\nnext row"
            if mode == "hostile"
            else 'column "price" does not exist'
        )
        good = Outcome(scenario_id=read, status="ok", body=[{"id": 1, "price": 5}])
        bad = Outcome(
            scenario_id=read,
            status="exception",
            exception_type="ProgrammingError",
            exception_message=message,
        )

        def cells(broken: bool, *, rollback: bool = True) -> list[CellResult]:
            changed = (
                good.model_copy(update={"body": [{"id": 99, "price": 6}]})
                if mode == "body_change"
                else bad
            )
            return [
                CellResult(
                    cell=Cell.A0D0, outcomes=[bad if mode == "coverage" else good]
                ),
                CellResult(cell=Cell.A0D1, outcomes=[changed if broken else good]),
                CellResult(
                    cell=Cell.A1D1, outcomes=[bad if mode == "head_failure" else good]
                ),
                CellResult(
                    cell=Cell.A0D1W,
                    outcomes=[
                        Outcome(
                            scenario_id=commit,
                            status="ok",
                            body={"created": True, "rolled_back": False},
                        ),
                        changed if broken and rollback else good,
                    ],
                ),
            ]

        policies = (
            {
                read: ComparisonPolicy(
                    compare_body=True, volatile_fields=frozenset({"id"})
                )
            }
            if mode == "body_change"
            else {}
        )
        initial_cells = cells(
            mode not in {"safe", "coverage", "head_failure"},
            rollback=mode not in {"replay_only", "body_change"},
        )
        initial = compare_matrix(initial_cells, policies=policies)
        attempts: list[ReproductionAttempt] = []
        if initial.candidates:
            for number in (1, 2):
                if mode == "inconclusive" and number == 1:
                    attempts.append(
                        ReproductionAttempt(number=number, error="database unavailable")
                    )
                else:
                    repeated = cells(
                        not (mode == "flaky" and number == 1),
                        rollback=mode != "body_change",
                    )
                    attempts.append(
                        ReproductionAttempt(
                            number=number,
                            cells=repeated,
                            comparison=compare_matrix(repeated, policies=policies),
                        )
                    )
        verification = VerificationResult(
            initial_cells=initial_cells,
            initial_comparison=initial,
            attempts=attempts,
            decisions=classify_reproductions(initial, attempts)
            if initial.candidates
            else [],
        )
        config = RunConfig(
            repo_path=tmp_path, base_ref="release-v1", head_ref="release-v2"
        )
        checkouts = CheckoutPair(
            base=tmp_path / "base",
            head=tmp_path / "head",
            base_sha="a" * 40,
            head_sha="b" * 40,
        )
        return build_report(
            config,
            checkouts,
            verification,
            settings_module="shop.settings",
            seed_fixture="products",
            policies=policies,
        )

    return make_report
