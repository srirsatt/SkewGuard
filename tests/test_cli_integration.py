"""Run the CLI against real Git releases, isolated environments, and PostgreSQL."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skewguard.cli import app
from skewguard.config import RunConfig
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.types import Cell
from skewguard.report.json_report import read_json_report
from skewguard.report.markdown import render_markdown

pytestmark = pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 to run tests requiring Docker",
)


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=SkewGuard Tests",
            "-c",
            "user.email=tests@example.invalid",
            "-C",
            str(repo),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.strip()


@pytest.mark.parametrize(
    ("head_ref", "exit_code", "confirmed", "http"),
    [("HEAD", 1, 4, False), ("main", 0, 0, False),
     ("HEAD", 1, 6, True), ("main", 0, 0, True)],
)
def test_cli_runs_clean_git_releases_writes_reports_and_cleans_up(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
    tmp_path: Path,
    head_ref: str,
    exit_code: int,
    confirmed: int,
    http: bool,
) -> None:
    _, releases = shop_releases
    repo = tmp_path / "django-repository"
    shutil.copytree(
        releases["base"].checkout,
        repo,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".skewguard-venv"),
    )
    git(repo, "init", "--initial-branch=main")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Add base Django release")
    base_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "candidate")
    for relative in (
        "catalog/migrations/0002_change.py",
        "catalog/models.py",
        "catalog/views.py",
    ):
        shutil.copyfile(releases["rename"].checkout / relative, repo / relative)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Rename product price")
    head_sha = git(repo, "rev-parse", head_ref)
    (repo / "local.txt").write_text("preserve local changes", encoding="utf-8")
    original_status = git(repo, "status", "--porcelain")
    original_worktrees = git(repo, "worktree", "list", "--porcelain")
    output = tmp_path / "reports/out.json"
    checkpoint = tmp_path / "checkpoints.sqlite"
    extra: list[str] = []
    if http:
        scenarios: list[dict[str, object]] = [
            {
                "scenario_id": "http:products:list",
                "method": "GET",
                "kind": "read",
                "path": "/products/",
                "compare_fields": ["products"],
            }
        ]
        if head_ref == "main":
            scenarios.append(
                {
                    "scenario_id": "http:products:create",
                    "method": "POST",
                    "kind": "write",
                    "path": "/products/",
                    "body": {"sku": "MUG", "name": "Mug", "price": 12},
                }
            )
        scenario_file = tmp_path / "scenarios.json"
        scenario_file.write_text(json.dumps(scenarios), encoding="utf-8")
        extra = ["--scenarios", str(scenario_file)]

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--head",
            head_ref,
            "--settings",
            "shop.settings",
            "--seed",
            "products",
            "--json",
            str(output),
            "--checkpoint",
            str(checkpoint),
            *extra,
        ],
    )

    assert result.exit_code == exit_code, f"{result.output}\n{result.exception}"
    assert "Starting: static analysis" in result.output
    assert "Completed: matrix and reproduction checks" in result.output
    identifier = re.search(r"Run ID: ([0-9a-f]{32})", result.output)
    assert identifier is not None
    report = read_json_report(output)
    assert report.context.base_sha == base_sha
    assert report.context.head_sha == head_sha
    assert report.summary.confirmed == confirmed
    assert len(report.verification.attempts) == (2 if confirmed else 0)
    if http:
        cells = {item.cell: item for item in report.verification.initial_cells}
        assert all(
            any(o.scenario_id == "http:products:list" for o in cell.outcomes)
            for cell in cells.values()
        )
        assert report.context.scenarios[0].compare_fields == ["products"]
        if head_ref == "main":
            rollback = cells[Cell.A0D1W]
            assert [o.http_status for o in rollback.rollback_writes] == [201]
            assert "http:products:create" not in {
                o.scenario_id for o in rollback.outcomes
            }
            old_read = cells[Cell.A0D0].rollback_controls[0]
            new_read = next(
                o for o in rollback.outcomes if o.scenario_id == "http:products:list"
            )
            assert old_read.body == new_read.body
            assert isinstance(new_read.body, dict)
            products = new_read.body["products"]
            assert isinstance(products, list)
            assert {"sku": "MUG", "name": "Mug", "price": 12, "stock": 0} in products
        else:
            assert {
                d.candidate.cell
                for d in report.verification.decisions
                if d.status == "confirmed"
                and d.candidate.scenario_id == "http:products:list"
            } == {Cell.A0D1, Cell.A0D1W}
    assert output.with_suffix(".md").read_text(encoding="utf-8") == render_markdown(
        report
    )
    assert git(repo, "status", "--porcelain") == original_status
    assert git(repo, "worktree", "list", "--porcelain") == original_worktrees
    assert (repo / "local.txt").read_text(encoding="utf-8") == "preserve local changes"
    assert not (repo / ".skewguard-venv").exists()

    rendered = CliRunner().invoke(app, ["report", str(output)])
    assert rendered.exit_code == 0
    assert rendered.output == render_markdown(report)

    resumed_path = tmp_path / "reports/resumed.json"
    resumed = CliRunner().invoke(app, [
        "resume", identifier.group(1), "--checkpoint", str(checkpoint),
        "--json", str(resumed_path),
    ])
    assert resumed.exit_code == exit_code, resumed.output
    assert read_json_report(resumed_path) == report
    assert "Starting:" not in resumed.output
