"""Run the CLI against real Git releases, isolated environments, and PostgreSQL."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skewguard.cli import app
from skewguard.config import RunConfig
from skewguard.exec.envs import PythonEnvironment
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
    ("head_ref", "exit_code", "confirmed"), [("HEAD", 1, 4), ("main", 0, 0)]
)
def test_cli_runs_clean_git_releases_writes_reports_and_cleans_up(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
    tmp_path: Path,
    head_ref: str,
    exit_code: int,
    confirmed: int,
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
        ],
    )

    assert result.exit_code == exit_code, f"{result.output}\n{result.exception}"
    report = read_json_report(output)
    assert report.context.base_sha == base_sha
    assert report.context.head_sha == head_sha
    assert report.summary.confirmed == confirmed
    assert len(report.verification.attempts) == (2 if confirmed else 0)
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
