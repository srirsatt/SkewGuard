"""Resume real migration verification after refs move and old resources disappear."""

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from skewguard.cli import app
from skewguard.exec.probes import ProbeExecutionError
from skewguard.report.json_report import read_json_report

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 to run tests requiring Docker",
)


def test_cli_resume_reuses_analysis_and_ignores_moved_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "demo"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/make_demo_repo.py"), str(repo)],
        check=True,
        capture_output=True,
        timeout=30,
    )

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()

    old_sha = git("rev-parse", "base")
    new_sha = git("rev-parse", "breaking")
    analysis_node = importlib.import_module("skewguard.graph.nodes.static_analyze")
    execution_node = importlib.import_module("skewguard.graph.nodes.execute_matrix")
    collector = Mock(wraps=analysis_node.collect_static_context)
    verifier = execution_node.verify_matrix
    monkeypatch.setattr(analysis_node, "collect_static_context", collector)
    monkeypatch.setattr(
        execution_node,
        "verify_matrix",
        Mock(side_effect=ProbeExecutionError("interrupted after analysis")),
    )
    checkpoint = tmp_path / "checkpoints.sqlite"
    output = tmp_path / "out.json"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo",
            str(repo),
            "--base",
            "base",
            "--head",
            "breaking",
            "--settings",
            "shop.settings",
            "--seed",
            "products",
            "--checkpoint",
            str(checkpoint),
            "--json",
            str(output),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "Completed: static analysis" in result.output
    assert not output.exists()
    identifier = re.search(r"Run ID: ([0-9a-f]{32})", result.output)
    assert identifier is not None

    git("update-ref", "refs/tags/base", "safe")
    git("update-ref", "refs/heads/breaking", "safe")
    before_status = git("status", "--porcelain")
    before_worktrees = git("worktree", "list", "--porcelain")
    monkeypatch.setattr(execution_node, "verify_matrix", verifier)
    resumed = CliRunner().invoke(
        app,
        [
            "resume",
            identifier.group(1),
            "--checkpoint",
            str(checkpoint),
            "--json",
            str(output),
        ],
    )

    assert resumed.exit_code == 1, f"{resumed.output}\n{resumed.exception}"
    assert "Starting: static analysis" not in resumed.output
    assert "Starting: matrix and reproduction checks" in resumed.output
    collector.assert_called_once()
    report = read_json_report(output)
    assert report.context.base_sha == old_sha
    assert report.context.head_sha == new_sha
    assert report.summary.confirmed == 4
    assert git("status", "--porcelain") == before_status
    assert git("worktree", "list", "--porcelain") == before_worktrees
